"""Process-bounded complete-redactor scaling for the known adversarial families."""

import json
import os
import subprocess
import sys

import pytest

_PROBE = (
    r"""
import ast, json, os, re, statistics, subprocess, sys, time
from sidegraph.capture import redact
if os.environ.get("SG100_REDACT_BASELINE"):
    source = subprocess.check_output(
        ["git", "show", "b37cfc4a:src/sidegraph/capture.py"], text=True
    )
    names = {"_SECRET_PATTERNS_BEFORE_ASSIGNMENTS", "_SECRET_PATTERNS_AFTER_ASSIGNMENTS",
        "_PAN_CANDIDATE", "_PAN_START", "_luhn_ok", "_ASSIGNMENT_KEY_NAME_SOURCE",
        "_ASSIGNMENT_KEY_NAME", "_ASSIGNMENT_KEYS", "_QUOTED_KEY_STOPS", "_LINE_BREAK",
        "_URL_SCHEME_SOURCE", "_URL_SCHEME_START", "_URL_CREDENTIAL",
        "_span_shadows_assignment", "_crossing_secret_end", "_assignment_value_end",
        "_redact_assignments", "redact"}
    selected = []
    for node in ast.parse(source).body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            selected.append(node)
    namespace = {"re": re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "<baseline>", "exec"), namespace)
    redact = namespace["redact"]
family, count = sys.argv[1], int(sys.argv[2])
text = {
    "key": lambda: "a-" * count + "token-a" * 2,
    "shadow": lambda: 'password:"' + "a-" * count + 'token = x" prose',
    "url": lambda: "a://u:" * count,
    "fields": lambda: '\"password\":\"x://u:\",' * count,
    "jwt": lambda: "eyJ-" * count,
    "pem": lambda: ("-----" + "BE_GIN PRIVATE KEY-----") * count,
}[family]()
samples = []
for _ in range(3):
    start = time.perf_counter()
    redact(text)
    samples.append(time.perf_counter() - start)
print(json.dumps({"median": statistics.median(samples), "bytes": len(text.encode())}))
"""
).replace("BE_GIN", "BEGIN")


def measure(family, count):
    process = subprocess.Popen(
        [sys.executable, "-c", _PROBE, family, str(count)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        output, error = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("bounded redactor scaling probe timed out; child killed and reaped")
    assert process.returncode == 0, error
    return json.loads(output)


@pytest.mark.slow
@pytest.mark.parametrize("family", ["key", "shadow", "url", "fields", "jwt", "pem"])
def test_complete_redactor_known_attack_scaling(family):
    smaller = measure(family, 4000)
    larger = measure(family, 16000)
    assert larger["bytes"] <= 8 * 1024 * 1024
    assert larger["median"] <= max(smaller["median"] * 7.5, 0.02)
