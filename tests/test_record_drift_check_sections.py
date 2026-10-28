"""record_drift_check with declared sections, through the MCP client."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sidegraph import drift_checks, server
from sidegraph.doctor import scan_code_drift
from sidegraph.drift_checks import BlobRead
from tests.drift_checks_support import (  # noqa: F401
    drift_ids,
    git,
    project,
    record_check,
    require_check,
)

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)

SPEC = (
    "# Spec\n\n## 30. Other\nx\n\n[r]: http://a\n\n## 55. Vocab\n\n### 55.2 Rules\nrule one\n\n"
    "## 56. Echo\n\n### 56.4 Amend\nsib\n"
)
FILES = ["code.py", "spec.md"]
SECTIONS = [{"file": "spec.md", "headings": [["55.2 Rules"]], "also_watch": [["56.4 Amend"]]}]


def write(root: Path, name: str, content: str | bytes) -> None:
    path = root / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")
    git(root, "add", "--", name)
    git(root, "commit", "-q", "-m", f"change {name}")


def snapshot(store) -> dict:
    return {p.relative_to(store.path): p.read_bytes() for p in store.path.rglob("*.json")}


def refused(result, store, before: dict, code: str) -> str:
    assert result.is_error
    text = result.content[0].text
    assert code in text, text
    assert snapshot(store) == before
    return text


@pytest.fixture
def spec_project(project):
    root, store, decision = project
    write(root, "spec.md", SPEC)
    return root, store, decision


def test_without_sections_behaves_as_before(project) -> None:
    root, store, decision = project
    payload = require_check(root, decision.id)
    assert "declared_sections" not in payload
    capsule = drift_checks.latest_checks(store.path)[decision.id].raw
    assert capsule["declared_sections"] == []


def test_declared_check_stores_hashes_and_echoes_readable_chains(spec_project) -> None:
    root, store, decision = spec_project
    payload = require_check(root, decision.id, files=FILES, sections=SECTIONS)
    assert payload["declared_sections"] == [
        {
            "path": "spec.md",
            "scheme": "md1",
            "total_lines": 16,
            "sections": [
                {
                    "heading": "Spec > 55. Vocab > 55.2 Rules",
                    "role": "declared",
                    "first_line": 10,
                    "lines": 3,
                },
                {
                    "heading": "Spec > 56. Echo > 56.4 Amend",
                    "role": "watch",
                    "first_line": 15,
                    "lines": 2,
                },
            ],
        }
    ]
    stored = drift_checks.latest_checks(store.path)[decision.id].raw["declared_sections"]
    assert [s["role"] for s in stored[0]["sections"]] == ["declared", "watch"]


def test_capsule_json_contains_no_heading_text(spec_project) -> None:
    root, store, decision = spec_project
    require_check(root, decision.id, files=FILES, sections=SECTIONS)
    selected = drift_checks.latest_checks(store.path)[decision.id]
    text = selected.path.read_text(encoding="utf-8")
    for heading in ("Vocab", "Rules", "Amend", "Echo"):
        assert heading not in text


def test_end_to_end_outside_edit_clean_inside_edit_rearms(spec_project) -> None:
    root, store, decision = spec_project
    require_check(root, decision.id, files=FILES, sections=SECTIONS)
    write(root, "spec.md", SPEC.replace("x\n", "xx\n", 1))
    assert drift_ids(store) == set()
    write(root, "spec.md", SPEC.replace("rule one", "rule two"))
    entries = [e for b in scan_code_drift(store.path).batches for e in b.entries]
    assert [(e.record_id, e.check_reason) for e in entries] == [
        (decision.id, "declared section changed")
    ]


# --- refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "files, sections, code",
    [
        (["code.py"], SECTIONS, "section_file_not_in_files"),
        (
            ["code.py"],
            [{"file": "code.py", "headings": [["policy"]]}],
            "section_file_not_markdown",
        ),
        (
            FILES,
            [
                {"file": "spec.md", "headings": [["55.2 Rules"]]},
                {"file": "spec.md", "headings": [["56.4 Amend"]]},
            ],
            "duplicate_section_file",
        ),
        (
            FILES,
            [{"file": "spec.md", "headings": [["55.2 Rules"], ["55.2 Rules"]]}],
            "duplicate_section",
        ),
        (
            FILES,
            [{"file": "spec.md", "headings": [["55.2 Rules"]], "also_watch": [["55.2 Rules"]]}],
            "duplicate_section",
        ),
        (
            FILES,
            [{"file": "spec.md", "headings": [["55. Vocab", "55.2 Rules"], ["55.2 Rules"]]}],
            "duplicate_section",
        ),
        (
            FILES,
            [
                {
                    "file": "spec.md",
                    "headings": [[f"h{n}"] for n in range(17)],
                    "also_watch": [[f"w{n}"] for n in range(16)],
                }
            ],
            "too_many_sections",
        ),
        (
            ["code.py", "a.md", "b.md", "c.md"],
            [
                {"file": name, "headings": [[f"{name}{n}"] for n in range(22)]}
                for name in ("a.md", "b.md", "c.md")
            ],
            "too_many_sections",
        ),
        (FILES, [{"file": "spec.md", "headings": [["Nope"]]}], "unknown_section"),
    ],
)
def test_input_refusals_publish_nothing(spec_project, files, sections, code: str) -> None:
    root, store, decision = spec_project
    before = snapshot(store)
    refused(record_check(root, decision.id, files=files, sections=sections), store, before, code)


def test_ambiguous_suffix_is_refused(project) -> None:
    root, store, decision = project
    write(root, "spec.md", "# T\n## A\n### Rules\n## B\n### Rules\n")
    before = snapshot(store)
    result = record_check(
        root, decision.id, files=FILES, sections=[{"file": "spec.md", "headings": [["Rules"]]}]
    )
    refused(result, store, before, "ambiguous_section")


@pytest.mark.parametrize(
    "content, code",
    [
        ("# T\n- ## in a list\n", "section_file_unsupported"),
        (b"# T\n\xff\xfe\n", "section_text_unavailable"),
        ("# T\n## Rules\nsee [^1] and [^a-z]\n", "section_file_unsupported"),
        ("# T\n## Rules\n" + "x\n" * 20_000, "section_file_too_large"),
        ("# T\n## Rules\n" + "y" * (2 * 1024 * 1024 + 1) + "\n", "section_file_too_large"),
    ],
    ids=["container", "not-utf8", "footnote", "too-many-lines", "too-many-bytes"],
)
def test_file_refusals_publish_nothing(project, content, code: str) -> None:
    root, store, decision = project
    write(root, "spec.md", content)
    before = snapshot(store)
    result = record_check(
        root, decision.id, files=FILES, sections=[{"file": "spec.md", "headings": [["Rules"]]}]
    )
    refused(result, store, before, code)


def test_scope_too_large_by_lines(project) -> None:
    root, store, decision = project
    for name in ("a.md", "b.md"):
        write(root, name, f"# {name}\n## Rules\n" + "x\n" * 12_000)
    before = snapshot(store)
    sections = [{"file": n, "headings": [["Rules"]]} for n in ("a.md", "b.md")]
    result = record_check(root, decision.id, files=["code.py", "a.md", "b.md"], sections=sections)
    refused(result, store, before, "section_scope_too_large")


def test_scope_too_large_by_bytes(project) -> None:
    root, store, decision = project
    names = [f"f{n}.md" for n in range(5)]
    for name in names:
        write(root, name, f"# {name}\n## Rules\n" + "z" * 1_900_000 + "\n")
    before = snapshot(store)
    sections = [{"file": n, "headings": [["Rules"]]} for n in names]
    result = record_check(root, decision.id, files=["code.py", *names], sections=sections)
    refused(result, store, before, "section_scope_too_large")


@pytest.mark.parametrize(
    "reason, code",
    [
        ("too_large", "section_file_too_large"),
        ("too_many_lines", "section_file_too_large"),
        ("scope_cap", "section_scope_too_large"),
        ("not_utf8", "section_text_unavailable"),
        ("unavailable", "section_text_unavailable"),
    ],
)
def test_blob_read_reason_maps_to_its_code(
    spec_project, monkeypatch, reason: str, code: str
) -> None:
    root, store, decision = spec_project
    monkeypatch.setattr(
        drift_checks,
        "read_blob_texts",
        lambda _root, oids, *a, **k: {oid: BlobRead(None, reason) for oid in oids},
    )
    before = snapshot(store)
    refused(record_check(root, decision.id, files=FILES, sections=SECTIONS), store, before, code)


# --- budget -------------------------------------------------------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(server, "time", fake)
    return fake


def test_blob_read_exhaustion_is_section_text_unavailable(spec_project, monkeypatch) -> None:
    root, store, decision = spec_project

    def exhausted(*a, **k):
        raise ValueError("Git check deadline exhausted")

    monkeypatch.setattr(drift_checks, "read_blob_texts", exhausted)
    before = snapshot(store)
    result = record_check(root, decision.id, files=FILES, sections=SECTIONS)
    refused(result, store, before, "section_text_unavailable")


def test_reserve_leaves_positive_remainder_below_one_second(
    spec_project, monkeypatch, clock
) -> None:
    root, store, decision = spec_project
    real = drift_checks.read_blob_texts

    def then_nearly_expire(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.now = 2.5  # the 3.0 s budget has 0.5 s left
        return result

    monkeypatch.setattr(drift_checks, "read_blob_texts", then_nearly_expire)
    before = snapshot(store)
    result = record_check(root, decision.id, files=FILES, sections=SECTIONS)
    refused(result, store, before, "section_text_unavailable")


def test_false_from_clean_inspected_files_is_told_apart_from_timeout(
    spec_project, monkeypatch, clock
) -> None:
    root, store, decision = spec_project
    monkeypatch.setattr(drift_checks, "clean_inspected_files", lambda *a, **k: False)
    before = snapshot(store)
    text = refused(
        record_check(root, decision.id, files=FILES, sections=SECTIONS),
        store,
        before,
        "invalid draft: files",
    )
    assert "section_text_unavailable" not in text

    def false_after_expiry(*a, **k):
        clock.now += 10.0
        return False

    monkeypatch.setattr(drift_checks, "clean_inspected_files", false_after_expiry)
    result = record_check(root, decision.id, files=FILES, sections=SECTIONS)
    refused(result, store, before, "section_text_unavailable")


def test_timeout_in_the_final_head_recheck_is_section_text_unavailable(
    spec_project, monkeypatch
) -> None:
    root, store, decision = spec_project
    real = drift_checks.head_revision
    calls = []

    def second_call_times_out(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise ValueError("Git check deadline exhausted")
        return real(*args, **kwargs)

    monkeypatch.setattr(drift_checks, "head_revision", second_call_times_out)
    before = snapshot(store)
    result = record_check(root, decision.id, files=FILES, sections=SECTIONS)
    refused(result, store, before, "section_text_unavailable")


@pytest.mark.slow
def test_nested_container_shape_within_real_budget(project) -> None:
    root, store, decision = project
    # "- > " x 3 per line over 20,000 lines is far over the token cap but cheap in bytes.
    write(root, "spec.md", "\n".join("- > " * 3 for _ in range(20_000)))
    before = snapshot(store)
    started = time.monotonic()
    result = record_check(
        root, decision.id, files=FILES, sections=[{"file": "spec.md", "headings": [["x"]]}]
    )
    assert result.is_error
    text = result.content[0].text
    assert "section_file_unsupported" in text or "section_text_unavailable" in text
    assert "invalid draft: files" not in text
    assert snapshot(store) == before
    assert time.monotonic() - started < 5.0


def test_stored_capsule_is_valid_json_with_the_field(spec_project) -> None:
    root, store, decision = spec_project
    require_check(root, decision.id, files=FILES, sections=SECTIONS)
    raw = json.loads(drift_checks.latest_checks(store.path)[decision.id].path.read_text())
    assert raw["declared_sections"][0]["scheme"] == "md1"
    assert raw["declared_sections"][0]["path"] == "spec.md"


def test_last_section_lines_use_the_total_lines_rule(project) -> None:
    root, store, decision = project
    write(root, "spec.md", "# A\nx\n")
    payload = require_check(
        root, decision.id, files=FILES, sections=[{"file": "spec.md", "headings": [["A"]]}]
    )
    echo = payload["declared_sections"][0]
    assert echo["total_lines"] == 2
    assert echo["sections"][0]["first_line"] == 1
    assert echo["sections"][0]["lines"] == 2


def test_heading_segment_whitespace_is_normalised(spec_project) -> None:
    root, store, decision = spec_project
    payload = require_check(
        root,
        decision.id,
        files=FILES,
        sections=[{"file": "spec.md", "headings": [["55.2  Rules"]]}],
    )
    assert payload["declared_sections"][0]["sections"][0]["heading"].endswith("55.2 Rules")


def test_identical_blobs_are_parsed_once(project, monkeypatch) -> None:
    root, store, decision = project
    names = [f"f{n}.md" for n in range(16)]
    for name in names:
        write(root, name, "# T\nbody\n")
    parsed: list[int] = []
    real = server.md_sections.parse_md1
    monkeypatch.setattr(
        server.md_sections, "parse_md1", lambda *a, **k: parsed.append(1) or real(*a, **k)
    )
    sections = [{"file": n, "headings": [["T"]]} for n in names]
    payload = require_check(root, decision.id, files=["code.py", *names], sections=sections)
    assert len(payload["declared_sections"]) == 16
    assert len(parsed) == 1


def test_expired_budget_is_refused_before_any_parse(spec_project, monkeypatch, clock) -> None:
    root, store, decision = spec_project
    write(root, "spec.md", "plain text, no headings\n")
    real = drift_checks.read_blob_texts

    def then_expire(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.now = 5.0
        return result

    parsed: list[int] = []
    real_parse = server.md_sections.parse_md1
    monkeypatch.setattr(drift_checks, "read_blob_texts", then_expire)
    monkeypatch.setattr(
        server.md_sections, "parse_md1", lambda *a, **k: parsed.append(1) or real_parse(*a, **k)
    )
    before = snapshot(store)
    result = record_check(
        root, decision.id, files=FILES, sections=[{"file": "spec.md", "headings": [["x"]]}]
    )
    refused(result, store, before, "section_text_unavailable")
    assert parsed == []


def test_overlapping_footnote_openers_do_not_stall_the_writer(project) -> None:
    root, store, decision = project
    write(root, "spec.md", "# T\n" + "[^" * 64_000 + "\n")
    started = time.monotonic()
    require_check(
        root, decision.id, files=FILES, sections=[{"file": "spec.md", "headings": [["T"]]}]
    )
    assert time.monotonic() - started < 2.0


def test_budget_expiring_mid_declaration_stops_before_the_next_footnote_scan(
    spec_project, monkeypatch, clock
) -> None:
    root, store, decision = spec_project
    scans: list[int] = []
    real = server.md_sections.footnote_ref

    def then_expire(*args, **kwargs):
        scans.append(1)
        clock.now = 5.0
        return real(*args, **kwargs)

    monkeypatch.setattr(server.md_sections, "footnote_ref", then_expire)
    before = snapshot(store)
    sections = [{"file": "spec.md", "headings": [["55.2 Rules"], ["56.4 Amend"]]}]
    result = record_check(root, decision.id, files=FILES, sections=sections)
    refused(result, store, before, "section_text_unavailable")
    assert len(scans) <= 1
