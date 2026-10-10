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


def test_redact_unterminated_single_quote_consumes_ambiguous_prose():
    clean, n = redact("api_key='sk-123 in the env; don't commit it")
    assert clean == "[REDACTED]"
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


@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
@pytest.mark.parametrize("payload", ["first secret tail", "first line\nsecond secret line"])
def test_redact_complete_quoted_value_and_preserve_following_prose(delimiter, payload):
    text = "password" + " = " + delimiter + payload + delimiter + " then prose"
    assert redact(text) == ("[REDACTED] then prose", 1)


@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
def test_redact_unterminated_value_consumes_all_secret_tail(delimiter):
    text = "token" + ": " + delimiter + "first secret\ntrailing secret at end"
    assert redact(text) == ("[REDACTED]", 1)


@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
def test_redact_escape_aware_delimiter_with_multiline_tail(delimiter):
    payload = "first " + "\\" + delimiter + " escaped\nsecret tail"
    text = "secret" + "=" + delimiter + payload + delimiter + " kept prose"
    assert redact(text) == ("[REDACTED] kept prose", 1)


@pytest.mark.parametrize("delimiter", ["'" * 3, '"' * 3, "`" * 3])
def test_redact_triple_value_glued_tail_consumed(delimiter):
    text = "api_key" + "=" + delimiter + "first secret tail" + delimiter + "glued kept"
    assert redact(text) == ("[REDACTED] kept", 1)


@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
def test_redact_quoted_key_multiline_preserves_json_neighbor(delimiter):
    text = '{"password":' + delimiter + "first\nsecret tail" + delimiter + ',"reason":"kept"}'
    assert redact(text) == ('{"[REDACTED],"reason":"kept"}', 1)


@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
def test_redact_empty_quoted_value_still_counted(delimiter):
    # Six adjacent quote characters are an empty triple-quoted value.
    text = "password" + "=" + delimiter + delimiter
    assert redact(text) == ("[REDACTED]", 1)


def test_redact_empty_bare_value_not_counted():
    text = "password" + "=   "
    assert redact(text) == (text, 0)


def test_redact_quoted_apostrophe_inside_value_is_not_a_closer():
    text = "password" + "='don't expose this secret' kept"
    assert redact(text) == ("[REDACTED] kept", 1)


@pytest.mark.parametrize(
    "text",
    [
        'password: "abc\napi_token: "xyz hunter2secret"',
        'password: "\ntoken: "hunter2 secretphrase"',
        "password: `abc\ntoken: `hunter2 secretphrase`",
        "password: 'abc\ntoken=' hunter2 secretphrase'",
        '{"password": "abc\n"token": "hunter2 secretphrase"}',
        r'password: "C:\temp\"' + '\ntoken: "abc hunter2secret"',
        r"token: `C:\dir\`" + "\nsecret: `abc hunter2secret`",
    ],
)
def test_redact_cross_line_false_closer_never_shadows_later_secret(text):
    clean, count = redact(text)
    assert "hunter2" not in clean
    assert "secretphrase" not in clean
    assert count >= 1


@pytest.mark.parametrize("first_quote", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
@pytest.mark.parametrize("second_quote", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
@pytest.mark.parametrize("quoted_key", [False, True])
@pytest.mark.parametrize("escape_tail", [False, True])
def test_redact_cross_line_assignment_compositions(
    first_quote,
    second_quote,
    quoted_key,
    escape_tail,
):
    first_key = '"password"' if quoted_key else "password"
    second_key = '"token"' if quoted_key else "token"
    tail = "path" + "\\" + first_quote if escape_tail else "first"
    marker = "syntheticsecond" + "secretmarker"
    text = first_key + ": " + first_quote + tail + "\n"
    text += second_key + ": " + second_quote + "first " + marker + second_quote
    clean, count = redact(text)
    assert marker not in clean
    assert count >= 1


@pytest.mark.parametrize(
    "line_break",
    ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
@pytest.mark.parametrize("quoted_key", [False, True])
def test_redact_false_closer_respects_all_line_boundaries(line_break, delimiter, quoted_key):
    first = '"password"' if quoted_key else "password"
    second = '"token"' if quoted_key else "token"
    marker = "syntheticsecond" + "secretmarker"
    text = first + ": " + delimiter + "first" + line_break
    text += second + ": " + delimiter + "first " + marker + delimiter
    clean, count = redact(text)
    assert marker not in clean
    assert count >= 1


@pytest.mark.parametrize(
    "line_break",
    ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
@pytest.mark.parametrize("delimiter", ["'", '"', "`"])
def test_redact_ordinary_multiline_value_preserves_prose_for_all_line_boundaries(
    line_break,
    delimiter,
):
    text = "password" + ": " + delimiter + "first" + line_break + "second" + delimiter + " kept"
    assert redact(text) == ("[REDACTED] kept", 1)


@pytest.mark.parametrize("key_quote", ["'", '"', "`"])
@pytest.mark.parametrize("separator", [" : ", " = ", "\t:\t", "\t=\t", "\n= ", "\r: "])
@pytest.mark.parametrize("line_break", ["\n", "\r", "\u2028"])
def test_redact_cross_line_key_separator_outside_consumed_span(key_quote, separator, line_break):
    marker = "syntheticsecond" + "secretmarker"
    text = "password" + ': "first' + line_break
    text += key_quote + "token" + key_quote + separator + '"first ' + marker + '"'
    clean, count = redact(text)
    assert marker not in clean
    assert count >= 1


@pytest.mark.parametrize("delimiter", ["'", '"', "`", "'" * 3, '"' * 3, "`" * 3])
@pytest.mark.parametrize("group_separator", [" ", "-"])
def test_redact_cross_line_false_closer_never_splits_luhn_pan(delimiter, group_separator):
    pan = group_separator.join(["4111", "1111", "1111", "1111"])
    text = (
        "password" + ": " + delimiter + "first\ncard " + delimiter + pan + delimiter + " declined"
    )
    clean, count = redact(text)
    assert "1111" not in clean
    assert count >= 1


def test_redact_invalid_grouped_pan_does_not_force_ambiguous_eof():
    pan = " ".join(["4111", "1111", "1111", "1112"])
    text = "password" + ': "first\norder "' + pan + '" declined'
    clean, count = redact(text)
    assert clean.endswith(' 1111 1111 1112" declined')
    assert count == 1


@pytest.mark.parametrize("punctuation", [",", ";", ")", "}", "]"])
@pytest.mark.parametrize("component", ["username", "password"])
def test_redact_cross_line_false_closer_never_splits_url_credentials(punctuation, component):
    username = "us" + punctuation + "ermarker" if component == "username" else "user"
    password = "pa" + punctuation + "ssmarker" if component == "password" else "passmarker"
    text = '{"password": "first\nsee "https://' + username + ":" + password + '@host"}'
    clean, count = redact(text)
    assert "ermarker" not in clean
    assert "ssmarker" not in clean
    assert "passmarker" not in clean
    assert count >= 1


@pytest.mark.parametrize(
    "text",
    [
        'token":"""my-secret,} "token""""4111 1111 1111 1111"',
        'password: "x"4111 1111 1111 1111 after prose',
        '{"password": """x"""https://u,v:p@h} after prose',
    ],
)
def test_singleline_glued_value_does_not_fragment_later_secret(text):
    clean, count = redact(text)
    assert "1111 1111 1111" not in clean
    assert ",v:p@" not in clean
    assert count >= 1
    if "after prose" in text:
        assert clean.endswith("after prose")


def test_singleline_crossing_pan_extension_covers_overlapping_valid_chain():
    clean, count = redact('password: "x"0002 1111 1111 1111 0002 after prose')
    assert "0002" not in clean
    assert "1111" not in clean
    assert clean.endswith("after prose")
    assert count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "password: 'it's 'token': 'abc def' done",
        "{'password': 'don't 'token': 'abc def'} done",
        "secret='o'reilly 'password': 'abc def' done",
    ],
)
def test_singleline_quote_shadow_does_not_hide_next_assignment_value(text):
    clean, count = redact(text)
    assert "abc def" not in clean
    assert count >= 1


def test_singleline_nested_assignment_signal_consumes_ambiguous_following_prose():
    clean, count = redact("password: 'my token: x' following prose")
    assert clean == "[REDACTED]"
    assert count == 1


@pytest.mark.parametrize(
    "text,tail",
    [
        ('{"password":https://u,v:p@h} kept', ",v:p@"),
        ("password:4111 1111 1111 1111 kept", "1111 1111 1111"),
        ("password:0002 1111 1111 1111 0002 kept", "0002"),
    ],
)
def test_bare_assignment_value_does_not_fragment_later_url_or_pan(text, tail):
    clean, count = redact(text)
    assert tail not in clean
    assert clean.endswith("kept")
    assert count >= 1


@pytest.mark.parametrize(
    "text,retained",
    [
        ("password:4111 1111 1111 1112 kept", "1111 1111 1112 kept"),
        ('{"password":https://u,v:p-no-at} kept', ",v:p-no-at} kept"),
        ("password:word following prose", "following prose"),
    ],
)
def test_bare_assignment_extension_preserves_nonsecret_prose(text, retained):
    assert redact(text)[0].endswith(retained)


def test_bare_assignment_shadow_does_not_hide_next_quoted_value():
    clean, count = redact("password=a_token: 'abc def' done")
    assert "abc def" not in clean
    assert count >= 1


@pytest.mark.parametrize("letter", ["é", "β", "文", "١", "ſ", "K"])
def test_unicode_alphanumeric_after_apostrophe_stays_inside_quoted_value(letter):
    text = "password: 'l'" + letter + "té secretmarker' following prose"
    clean, count = redact(text)
    assert clean == "[REDACTED] following prose"
    assert count == 1


def test_unicode_alphanumeric_glued_to_single_quote_consumed_conservatively():
    assert redact("password: 'value'文 following prose") == ("[REDACTED]", 1)


@pytest.mark.parametrize("key_quote", ["'", '"', "`"])
@pytest.mark.parametrize("value_quote", ["'", '"', "`"])
@pytest.mark.parametrize("stop", [",", ";", ")", "}", "]"])
def test_expanded_url_span_does_not_swallow_next_quoted_assignment_key(
    key_quote, value_quote, stop
):
    text = (
        '"password":"x"https://user'
        + stop
        + key_quote
        + "token"
        + key_quote
        + ":"
        + value_quote
        + "abc@ zzexpandedmarker"
        + value_quote
        + " following prose"
    )
    clean, count = redact(text)
    assert "zzexpandedmarker" not in clean
    assert count >= 1


@pytest.mark.parametrize("stop", [",", ";", ")", "}", "]"])
def test_expanded_url_without_assignment_key_preserves_following_prose(stop: str) -> None:
    text = '"password":"x"https://user' + stop + "name:abc@ following prose"
    clean, count = redact(text)
    assert clean == '"[REDACTED] following prose'
    assert count == 1
