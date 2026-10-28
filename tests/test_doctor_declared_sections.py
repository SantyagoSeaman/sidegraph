"""Declared sections: the drift scan compares sections instead of the whole blob."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph import doctor, drift_checks, md_sections
from sidegraph.doctor import scan_code_drift
from sidegraph.md_sections import parse_md1, resolve_suffix, section_text_hash
from sidegraph.schema import DeclaredSection, DeclaredSectionFile
from tests.drift_checks_support import (  # noqa: F401
    drift_ids,
    git,
    project,
    require_check,
)

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)

SPEC = (
    "# Spec\n\n## 30. Other\nx\n\n[r]: http://a\n\n## 55. Vocab\n\n### 55.2 Rules\nrule one\n\n"
    "## 56. Echo\n\n### 56.4 Amend\nsib\n"
)
RULES = [["55.2 Rules"]]
AMEND = [["56.4 Amend"]]


def write(root: Path, name: str, content: str | bytes) -> None:
    path = root / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")
    git(root, "add", "--", name)
    git(root, "commit", "-q", "-m", f"change {name}")


def section_entry(
    text: str,
    declared=RULES,
    watch=(),
    *,
    path: str = "spec.md",
    scheme: str = "md1",
    extra_role: str | None = None,
) -> DeclaredSectionFile:
    doc = parse_md1(text)
    sections = []
    for role, group in (("declared", declared), ("watch", watch)):
        for suffix in group:
            heading = resolve_suffix(doc, suffix)
            sections.append(
                DeclaredSection(
                    heading_hash=heading.hash, role=role, text_hash=section_text_hash(doc, heading)
                )
            )
    if extra_role:
        sections.append(DeclaredSection(heading_hash="ab", role=extra_role, text_hash="cd"))
    return DeclaredSectionFile(path=path, scheme=scheme, refs_hash=doc.refs_hash, sections=sections)


_STAMP = [0]


def publish(store, root, decision, entries, files=("code.py", "spec.md")):
    """A plain check through the real writer, then the same capsule with declared sections
    under a later id (the latest id is the one the scan consults)."""
    require_check(root, decision.id, files=list(files))
    plain = drift_checks.latest_checks(store.path)[decision.id].validated()
    _STAMP[0] += 10
    later = ULID.from_datetime(datetime.now(UTC) + timedelta(seconds=_STAMP[0]))
    store.record_drift_check(
        plain.model_copy(update={"id": str(later), "declared_sections": entries})
    )


@pytest.fixture
def declared(project):
    """A repo whose spec.md is checked with RULES declared; returns the helpers."""
    root, store, decision = project
    write(root, "spec.md", SPEC)
    publish(store, root, decision, [section_entry(SPEC)])
    return root, store, decision


def verdict(store, decision) -> tuple[str | None, list[str]]:
    for batch in scan_code_drift(store.path).batches:
        for entry in batch.entries:
            if entry.record_id == decision.id:
                return entry.check_reason, entry.files
    return None, []


# --- red on the whole-blob comparison -----------------------------------------------------


def test_edit_outside_declared_section_is_clean(declared) -> None:
    root, store, decision = declared
    write(root, "spec.md", SPEC.replace("x\n", "xx\n", 1))
    assert verdict(store, decision) == (None, [])


def test_sibling_edit_without_watch_is_clean(declared) -> None:
    root, store, decision = declared
    write(root, "spec.md", SPEC.replace("sib", "sib2"))
    assert verdict(store, decision) == (None, [])


def test_crlf_conversion_of_whole_file_is_clean(declared) -> None:
    root, store, decision = declared
    write(root, "spec.md", SPEC.replace("\n", "\r\n"))
    assert verdict(store, decision) == (None, [])


# --- guards -------------------------------------------------------------------------------


def test_declared_section_changed(declared) -> None:
    root, store, decision = declared
    write(root, "spec.md", SPEC.replace("rule one", "rule two"))
    assert verdict(store, decision) == ("declared section changed", ["spec.md"])


def test_also_watched_section_changed(project) -> None:
    root, store, decision = project
    write(root, "spec.md", SPEC)
    publish(store, root, decision, [section_entry(SPEC, RULES, AMEND)])
    write(root, "spec.md", SPEC.replace("sib", "sib2"))
    assert verdict(store, decision) == ("also-watched section changed", ["spec.md"])


@pytest.mark.parametrize("old", ["55.2 Rules", "## 55. Vocab"])
def test_declared_section_not_found_after_rename(declared, old: str) -> None:
    root, store, decision = declared
    write(root, "spec.md", SPEC.replace(old, old + " renamed"))
    assert verdict(store, decision) == ("declared section not found", ["spec.md"])


def test_referenced_link_definition_changed(declared) -> None:
    root, store, decision = declared
    write(root, "spec.md", SPEC.replace("http://a", "http://b"))
    assert verdict(store, decision) == ("referenced link definition changed", ["spec.md"])


# --- fallbacks (each is the whole-file verdict) -------------------------------------------

UNAVAILABLE = "section scope unavailable; whole file compared"


def _swap(root, content):
    write(root, "spec.md", content)


def _bytes(root):
    write(root, "spec.md", b"# Spec\n\xff\xfe\n")


def _chmod(root):
    (root / "spec.md").chmod(0o755)
    git(root, "add", "--", "spec.md")
    git(root, "update-index", "--chmod=+x", "spec.md")
    git(root, "commit", "-q", "-m", "mode")


def _blob_and_mode(root):
    write(root, "spec.md", SPEC.replace("x\n", "xx\n", 1))
    git(root, "update-index", "--chmod=+x", "spec.md")
    git(root, "commit", "-q", "-m", "mode")


def _remove(root):
    git(root, "rm", "-q", "spec.md")
    git(root, "commit", "-q", "-m", "remove")


EDIT_OUTSIDE = SPEC.replace("x\n", "xx\n", 1)


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda r: _swap(r, "# Spec\n" + "x\n" * 20_000), UNAVAILABLE),  # 20,001 lines
        (lambda r: _swap(r, "".join(f"# h{i}\n" for i in range(5001))), UNAVAILABLE),
        (lambda r: _swap(r, "# Spec\n" + "y" * (2 * 1024 * 1024 + 1) + "\n"), UNAVAILABLE),
        (lambda r: _swap(r, SPEC + "- ## in a list item\n"), UNAVAILABLE),
        (lambda r: _swap(r, SPEC + "> ## in a quote\n"), UNAVAILABLE),
        (_bytes, UNAVAILABLE),
        (_remove, "checked files changed"),
        (_chmod, "checked files changed"),
        (_blob_and_mode, "checked files changed"),
    ],
    ids=[
        "line-cap",
        "heading-cap",
        "byte-cap",
        "list-heading",
        "quote-heading",
        "not-utf8",
        "absent",
        "mode-only",
        "blob-and-mode",
    ],
)
def test_fallback_is_the_whole_file_verdict(declared, change, expected: str) -> None:
    root, store, decision = declared
    change(root)
    assert verdict(store, decision) == (expected, ["spec.md"])


def test_line_cap_is_refused_before_the_parser_is_built(declared, monkeypatch) -> None:
    import markdown_it

    root, store, decision = declared
    write(root, "spec.md", "# Spec\n" + "x\n" * 20_000)

    def boom(*a, **k):
        raise AssertionError("parser constructed")

    monkeypatch.setattr(markdown_it, "MarkdownIt", boom)
    assert verdict(store, decision) == (UNAVAILABLE, ["spec.md"])


def test_scan_caps_fall_back(declared, monkeypatch) -> None:
    root, store, decision = declared
    write(root, "spec.md", EDIT_OUTSIDE)
    monkeypatch.setattr(drift_checks, "SCAN_MAX_LINES", 3)
    assert verdict(store, decision) == (UNAVAILABLE, ["spec.md"])
    monkeypatch.setattr(drift_checks, "SCAN_MAX_LINES", 60_000)
    monkeypatch.setattr(drift_checks, "SCAN_MAX_BYTES", 10)
    assert verdict(store, decision) == (UNAVAILABLE, ["spec.md"])


def test_failed_read_falls_back(declared, monkeypatch) -> None:
    root, store, decision = declared
    write(root, "spec.md", EDIT_OUTSIDE)

    def broken(*a, **k):
        raise ValueError("Git check unavailable")

    monkeypatch.setattr(drift_checks, "read_blob_texts", broken)
    assert verdict(store, decision) == (UNAVAILABLE, ["spec.md"])


def test_parser_exception_falls_back(declared, monkeypatch) -> None:
    root, store, decision = declared
    write(root, "spec.md", EDIT_OUTSIDE)

    def broken(*a, **k):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(md_sections, "parse_md1", broken)
    assert verdict(store, decision) == (UNAVAILABLE, ["spec.md"])


class FakeTime:
    """The doctor's clock, advanced by hand; everything else is the real module."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.mark.parametrize("expire_after", ["tree_entries", "read_blob_texts"])
def test_scan_budget_exhausted_before_the_parse_falls_back(
    declared, monkeypatch, expire_after: str
) -> None:
    root, store, decision = declared
    write(root, "spec.md", EDIT_OUTSIDE)
    clock = FakeTime()
    monkeypatch.setattr(doctor, "time", clock)
    real = getattr(drift_checks, expire_after)

    def then_expire(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.now += 100.0
        return result

    parsed: list[int] = []
    real_parse = md_sections.parse_md1
    monkeypatch.setattr(drift_checks, expire_after, then_expire)
    monkeypatch.setattr(
        md_sections, "parse_md1", lambda *a, **k: parsed.append(1) or real_parse(*a, **k)
    )
    scan = scan_code_drift(store.path, deadline=50.0)
    entries = [e for b in scan.batches for e in b.entries if e.record_id == decision.id]
    assert [(e.check_reason, e.files) for e in entries] == [(UNAVAILABLE, ["spec.md"])]
    assert parsed == []


# --- forward compatibility through the real scan ------------------------------------------


def test_unknown_scheme_and_unknown_roles_compare_by_whole_blob(project) -> None:
    root, store, decision = project
    write(root, "spec.md", SPEC)
    swift = DeclaredSectionFile.model_validate(
        {
            "path": "spec.md",
            "scheme": "swift1",
            "future": 1,
            "sections": [{"heading_hash": "ab", "role": "region", "text_hash": "cd"}],
        }
    )
    publish(store, root, decision, [swift])
    write(root, "spec.md", EDIT_OUTSIDE)
    assert verdict(store, decision) == ("checked files changed", ["spec.md"])


@pytest.mark.parametrize("with_declared", [True, False])
def test_md1_entry_with_unknown_role_compares_by_whole_blob(project, with_declared: bool) -> None:
    root, store, decision = project
    write(root, "spec.md", SPEC)
    if with_declared:
        entry = section_entry(SPEC, RULES, extra_role="region")
    else:
        entry = DeclaredSectionFile(
            path="spec.md",
            scheme="md1",
            sections=[DeclaredSection(heading_hash="ab", role="region", text_hash="cd")],
        )
    publish(store, root, decision, [entry])
    write(root, "spec.md", EDIT_OUTSIDE)
    assert verdict(store, decision) == ("checked files changed", ["spec.md"])


# --- reason precedence and files membership -----------------------------------------------


@pytest.fixture
def mixed(project):
    root, store, decision = project
    write(root, "spec.md", SPEC)
    write(root, "dependency.py", "dependency = 1\n")
    publish(
        store,
        root,
        decision,
        [section_entry(SPEC, RULES, AMEND)],
        files=("code.py", "spec.md", "dependency.py"),
    )
    return root, store, decision


def test_plain_file_changed_declared_equal_lists_only_the_plain_file(mixed) -> None:
    root, store, decision = mixed
    write(root, "spec.md", EDIT_OUTSIDE)
    write(root, "dependency.py", "dependency = 2\n")
    assert verdict(store, decision) == ("checked files changed", ["dependency.py"])


def test_both_changed_lists_both(mixed) -> None:
    root, store, decision = mixed
    write(root, "spec.md", SPEC.replace("rule one", "rule two"))
    write(root, "dependency.py", "dependency = 2\n")
    assert verdict(store, decision) == ("checked files changed", ["dependency.py", "spec.md"])


def test_only_declared_changed_lists_the_declared_file(mixed) -> None:
    root, store, decision = mixed
    write(root, "spec.md", SPEC.replace("rule one", "rule two"))
    assert verdict(store, decision) == ("declared section changed", ["spec.md"])


def test_several_causes_follow_the_spec_order(mixed) -> None:
    root, store, decision = mixed
    text = SPEC.replace("rule one", "R").replace("http://a", "http://b").replace("sib", "S")
    write(root, "spec.md", text)
    assert verdict(store, decision)[0] == "declared section changed"
    write(root, "spec.md", text.replace("### 55.2 Rules", "### 55.2 Laws"))
    assert verdict(store, decision)[0] == "declared section not found"
    write(root, "spec.md", SPEC.replace("http://a", "http://b").replace("sib", "S"))
    assert verdict(store, decision)[0] == "referenced link definition changed"


def test_not_found_outranks_changed_in_the_same_record(project) -> None:
    root, store, decision = project
    write(root, "spec.md", SPEC)
    publish(store, root, decision, [section_entry(SPEC, RULES + [["30. Other"]])])
    write(root, "spec.md", SPEC.replace("55.2 Rules", "55.2 Laws").replace("x\n", "xx\n", 1))
    assert verdict(store, decision) == ("declared section not found", ["spec.md"])


def test_plain_change_outranks_unavailable_scope_in_the_same_record(mixed) -> None:
    root, store, decision = mixed
    write(root, "spec.md", SPEC + "- ## in a list item\n")
    write(root, "dependency.py", "dependency = 2\n")
    assert verdict(store, decision) == ("checked files changed", ["dependency.py", "spec.md"])


def test_scope_hashes_each_section_once_and_stops_after_expiry(declared, monkeypatch) -> None:
    root, store, decision = declared
    write(root, "spec.md", EDIT_OUTSIDE)
    oid = git(root, "rev-parse", "HEAD:spec.md")
    entry = section_entry(SPEC)
    left = [5.0]
    scope = doctor._SectionScope(root, {oid}, lambda: left[0])
    calls: list[int] = []
    real = md_sections.section_text_hash
    monkeypatch.setattr(
        md_sections, "section_text_hash", lambda d, h: calls.append(1) or real(d, h)
    )
    for _ in range(200):
        assert scope.compare(oid, entry) == []
    assert len(calls) == 1
    left[0] = 0.0
    assert scope.compare(oid, entry) is None
    assert len(calls) == 1


# --- call counts --------------------------------------------------------------------------

# Captured by running the scan below on the code before declared sections existed.
PLAIN_SCAN_GIT_CALLS = [
    ["rev-parse", "--show-toplevel"],
    ["diff", "--name-only"],
    ["rev-parse", "HEAD"],
    ["--literal-pathspecs", "ls-tree"],
]


def _git_calls(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(args, *a, **k):
        if args and args[0] == "git":
            calls.append(list(args[1:3]))
        return real(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    return calls


def test_no_declared_sections_makes_the_same_git_calls(project, monkeypatch) -> None:
    root, store, decision = project
    require_check(root, decision.id)
    write(root, "unrelated.py", "x = 1\n")
    calls = _git_calls(monkeypatch)
    assert drift_ids(store) == set()
    assert calls == PLAIN_SCAN_GIT_CALLS


def test_plain_scan_imports_no_markdown_parser(project, tmp_path) -> None:
    root, store, decision = project
    require_check(root, decision.id)
    src = Path(drift_checks.__file__).resolve().parent.parent
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "import sidegraph\n"
        f"assert sidegraph.__file__.startswith({str(src)!r}), sidegraph.__file__\n"
        "from sidegraph.doctor import scan_code_drift\n"
        "scan_code_drift(sys.argv[1])\n"
        "print('markdown_it' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", script, str(store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False", out.stderr


def test_many_records_on_one_spec_cost_one_read_one_parse(project, monkeypatch) -> None:
    root, store, decision = project
    write(root, "spec.md", SPEC)
    entry = section_entry(SPEC)
    publish(store, root, decision, [entry])
    for n in range(38):
        other = store.add_decision(
            decision.model_copy(update={"id": f"other{n}", "title": f"Rule {n}"})
        )
        for binding in store.bindings_for_record(decision.id):
            store.add_binding(binding.model_copy(update={"record_id": other.id}))
        publish(store, root, other, [entry])
    write(root, "spec.md", EDIT_OUTSIDE)
    parsed: list[int] = []
    real_parse = md_sections.parse_md1
    monkeypatch.setattr(
        md_sections, "parse_md1", lambda *a, **k: parsed.append(1) or real_parse(*a, **k)
    )
    calls = _git_calls(monkeypatch)
    assert drift_ids(store) == set()
    cat_file = [c for c in calls if c[0] == "cat-file"]
    assert cat_file == [["cat-file", "--batch-check"], ["cat-file", "--batch"]]
    assert len(parsed) == 1
