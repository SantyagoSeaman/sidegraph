"""Immutable legacy regex oracles; production scanners must retain their windows."""

import re

import pytest

NAME = re.compile(r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|secret|password)")
KEYS = (
    re.compile(NAME.pattern + r"(?:[_-][a-z0-9]+)*\s*[=:]\s*"),
    re.compile(NAME.pattern + r"(?:[_-][a-z0-9]+)*[\"'`]\s*[=:]\s*"),
)
SCHEME = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://")
URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):([^@\s]+)@")


def scanner(text):
    from sidegraph.redaction_scan import PhaseScanner

    return PhaseScanner(text)


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=x token=abc",
        '"prefix-token-password" : x',
        "a--token=x a__token=y a__prefix-token=z",
        "foo-token-password = x",
        "é-token=x é_token=x İAPI_KEY=x paſſword=x",
        "tokenizers=x apikeyfoo=x",
        "token-hello-world= x api-key-token=x 12-secret=x",
        "token_ = x",
        "foo-password_tokenizer=x",
        "apikey-key=x",
    ],
)
def test_assignment_scanner_matches_immutable_full_key_oracle(text):
    for quoted_key, pattern in enumerate(KEYS):
        assert list(scanner(text).assignments(quoted_key=bool(quoted_key))) == [
            (hit.start(), hit.end()) for hit in pattern.finditer(text)
        ]


@pytest.mark.parametrize(
    "text",
    [
        "foo-token-password = x",
        "a--token=x a__prefix-token=z",
        "tokenizer-token=x",
        "é_token=x _token=x -token=x",
        "api-key-secret=x",
        "api-keyx-token =x",
        '"token"=x password=x',
        "foo-token---secret=x",
        "nothing-else=x",
    ],
)
def test_name_windows_and_uncapped_full_key_check_match_original_oracle(text):
    phase = scanner(text)
    for start in range(len(text) + 1):
        for end in range(start, len(text) + 1):
            want = [(h.start(), h.end()) for h in NAME.finditer(text, start, end)]
            assert list(phase.name_spans(start, end)) == want, (text, start, end)
            shadow = any(any(p.match(text, h[0]) for p in KEYS) for h in want)
            assert phase.shadows_assignment(start, end) == shadow, (text, start, end)


def test_name_greedy_prefix_falls_back_inside_truncated_run():
    phase = scanner("foo-token-password = x")
    assert list(phase.name_spans(0, 9)) == [(0, 9)]
    assert phase.shadows_assignment(0, 9)


@pytest.mark.parametrize(
    "text",
    [
        "a-" * 20,
        "http://u:p@host",
        "a://u:p/a:b@h",
        "1a://u:p@",
        "éhttp://u:p@ _http://u:p@ -http://u:p@",
        "İ://u:p@ K://u:p@",
        "a://u:@ a://:p@ a://u:p x://u:p@",
        "a://b:a://b:tail@",
        "a://b:a://b:tail",
        "a://u:p a://u:q@",
        "a://u/p:q@ a://u@p:q@",
        "a://'u':'p',x@",
    ],
)
def test_url_and_all_bounded_scheme_windows_match_immutable_oracles(text):
    phase = scanner(text)
    assert list(phase.url_spans()) == [h.span() for h in URL.finditer(text)]
    for start in range(len(text) + 1):
        for end in range(start, len(text) + 1):
            assert list(phase.scheme_starts(start, end)) == [
                h.start() for h in SCHEME.finditer(text, start, end)
            ], (text, start, end)
    for hit in SCHEME.finditer(text):
        full = URL.match(text, hit.start())
        assert phase.credential_end(hit.start()) == (full.end() if full else None)


def test_seeded_key_and_url_differential_with_no_key_controls():
    import random

    rng = random.Random(100)
    pieces = [
        "a",
        "api",
        "key",
        "apikey",
        "token",
        "tokenizer",
        "secret",
        "password",
        "İ",
        "ı",
        "ſ",
        "K",
        "é",
        "_",
        "-",
        "--",
        "__",
        " = ",
        '"',
        "'",
        "`",
        "://",
        "@",
        ":",
        "/",
        " ",
        "\n",
        ",",
        "1",
        "9",
        "+",
        ".",
    ]
    for _ in range(400):
        text = "".join(rng.choices(pieces, k=rng.randrange(1, 25)))
        for quoted, pattern in enumerate(KEYS):
            assert list(scanner(text).assignments(quoted_key=bool(quoted))) == [
                h.span() for h in pattern.finditer(text)
            ], text
        phase = scanner(text)
        assert list(phase.url_spans()) == [h.span() for h in URL.finditer(text)], text
        for _ in range(12):
            start = rng.randrange(len(text) + 1)
            end = rng.randrange(start, len(text) + 1)
            names = [(h.start(), h.end()) for h in NAME.finditer(text, start, end)]
            assert list(phase.name_spans(start, end)) == names, (text, start, end)
            assert list(phase.scheme_starts(start, end)) == [
                h.start() for h in SCHEME.finditer(text, start, end)
            ], (text, start, end)


def _scaling_worker(connection, case, sizes):
    import statistics
    import time

    try:
        timings = []
        for size in sizes:
            samples = []
            for _ in range(3):
                if case == "plain":
                    text = "a-" * size
                elif case == "shadow":
                    text = '"' + "prefix-" * size + "\n" + "prefix-" * size + '"'
                elif case == "malformed":
                    text = "a://b:" * size + "tail"
                else:
                    text = '"token":"a://b:p",' * size
                begin = time.perf_counter()
                phase = scanner(text)
                if case == "plain":
                    assert list(phase.assignments()) == []
                    assert list(phase.url_spans()) == []
                elif case == "shadow":
                    assert not phase.shadows_assignment(0, len(text))
                elif case == "malformed":
                    assert list(phase.url_spans()) == []
                else:
                    # Increasing disjoint value windows share one text phase and
                    # one password-stop cache; comma/quote do not stop a password.
                    width = len('"token":"a://b:p",')
                    for start in range(0, len(text), width):
                        for hit in phase.scheme_starts(start, start + width):
                            assert phase.credential_end(hit) is None
                        assert not phase.shadows_assignment(start + 9, start + width)
                samples.append(time.perf_counter() - begin)
            timings.append(statistics.median(samples))
        connection.send((True, timings))
    except BaseException as error:
        connection.send((False, repr(error)))
    finally:
        connection.close()


@pytest.mark.slow
@pytest.mark.parametrize("case", ["plain", "shadow", "malformed", "fields"])
def test_scanner_scaling_runs_in_killable_reaped_process(case):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_scaling_worker, args=(send, case, (4000, 16000)))
    process.start()
    send.close()
    try:
        assert receive.poll(12), f"{case} scanner exceeded hard process deadline"
        success, result = receive.recv()
        assert success, result
        small, large = result
        assert large < 7 * small + 0.03, (case, result)
        process.join(1)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
        process.join()
        receive.close()
