"""SessionStart output size budget: a large store's map is trimmed to fit the smallest host's
hook output limits (Claude Code's 10,000-character context, Muse's 16 KiB output) instead of
being lost whole."""

import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.retrieval import MapEntry, MapSection, render_sections

MUSE_CAP = 16 * 1024


def _hook(monkeypatch, capsys, db, graph) -> bytes:
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    return capsys.readouterr().out.encode("utf-8")


def _context(raw: bytes) -> str:
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]


def _assert_within_limits(raw: bytes) -> None:
    out = json.loads(raw)
    assert len(raw) <= hooks.SESSION_START_OUTPUT_BUDGET < MUSE_CAP
    context = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert len(context) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert len(out.get("systemMessage", "")) <= hooks.HOOK_CONTEXT_MAX_CHARS


def test_ascii_map_is_trimmed_under_the_character_cap():
    """An ASCII map can fill the context before it fills the complete hook output."""
    heading = "## Unratified proposals"
    lines = [
        "- [gotcha] [unratified] Handle payments\n## Implementation notes",
        *[f"- [gotcha] [unratified] proposal {i} {'x' * 1000}" for i in range(11)],
    ]
    sections = _sections((heading, lines))
    full = render_sections(sections)
    assert len(full) > 10_000
    assert len(json.dumps(full).encode()) < hooks.SESSION_START_OUTPUT_BUDGET - 1024

    raw = hooks._fit_session_output("head\n\n", sections, [], []).encode() + b"\n"

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert hooks.HOOK_CONTEXT_MAX_CHARS - 1024 < len(ctx) <= hooks.HOOK_CONTEXT_MAX_CHARS
    kept = [line for line in ctx.splitlines() if line.startswith("- [gotcha]")]
    full_entries = [line for line in full.splitlines() if line.startswith("- [gotcha]")]
    assert 1 < len(kept) < len(lines)
    assert kept == full_entries[: len(kept)]  # the tail goes, without clipping lines
    assert kept[0] == "- [gotcha] [unratified] Handle payments ## Implementation notes"
    assert "\n## Implementation notes" not in ctx
    last = ctx.splitlines()[-1]
    assert last.startswith(f"Sidegraph: {len(lines) - len(kept)} map entries omitted")
    assert "list_proposed" in last


def test_one_oversized_entry_does_not_empty_its_section():
    heading = "## Unratified proposals"
    small = [f"- [gotcha] [unratified] proposal {i}" for i in range(100)]
    sections = _sections((heading, ["- " + "huge " * 2400, *small]))

    raw = hooks._fit_session_output("head\n\n", sections, [], []).encode() + b"\n"

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert all(line in ctx.splitlines() for line in small)
    assert (
        "Sidegraph: 1 map entry omitted to fit the hook output limit: "
        "1 unratified proposal (list_proposed)."
    ) in ctx.splitlines()


def test_the_fallback_map_trims_proposals_then_facts_then_decisions(tmp_path, monkeypatch, capsys):
    """The map without named domains, from a real store: proposals go, recorded decisions are
    trimmed, global mistakes stay. (Short facts can be put back into the room left.)"""
    from sidegraph.schema import Decision, DecisionStatus, Fact, Provenance, Scope
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    now = datetime.now(UTC)
    pad = "w" * 400

    def decision(n: int, kind: str, **extra) -> Decision:
        return Decision(
            title=f"{kind} {n} {pad}",
            kind=kind,
            context="c",
            choice="c",
            valid_from=now - timedelta(minutes=n),
            provenance=Provenance(source="manual"),
            **extra,
        )

    for n in range(10):
        store.add_decision(
            decision(n, "gotcha", scope=Scope.GLOBAL, status=DecisionStatus.ACCEPTED)
        )
        store.add_decision(decision(n, "adr", status=DecisionStatus.ACCEPTED))
        store.add_decision(decision(n, "lesson", status=DecisionStatus.PROPOSED))
        store.add_fact(
            Fact(
                statement=f"fact {n} {pad}",
                source="bench",
                status=DecisionStatus.ACCEPTED,
                valid_from=now - timedelta(minutes=n),
                provenance=Provenance(source="manual"),
            )
        )
    store.close()

    raw = _hook(monkeypatch, capsys, db, tmp_path / "no-graph.json")

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert "## Unratified proposals" not in ctx
    assert ctx.count("[gotcha]") == 10
    decisions = sum(1 for line in ctx.split("\n") if line.startswith("- [adr]"))
    assert 0 < decisions < 10
    last = ctx.split("\n")[-1]
    assert "10 unratified proposals (list_proposed)" in last
    assert f"{10 - decisions} decision" in last and 'retrieve_decisions(status="accepted")' in last


def _short_base(request) -> Path:
    """A fresh directory under ``/tmp``: SQLite refuses a path over 512 bytes and the pytest
    base directory on macOS is long, so a test of a long project path starts from a short one."""
    base = Path(tempfile.mkdtemp(prefix="sg", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    return base


# Windows keeps paths under 260 characters by default: these paths cannot exist there.
posix_long_paths = pytest.mark.skipif(os.name == "nt", reason="long paths need a POSIX file system")


@posix_long_paths
def test_a_long_unicode_project_path_with_a_stray_store_stays_within_limits(
    monkeypatch, capsys, request
):
    """The protected lines alone (status lines and notices naming a long non-ASCII path) can
    break the byte budget; the hook then drops the map and clips them, and still prints."""
    from sidegraph.schema import Decision, Provenance
    from sidegraph.store import Store

    root = _short_base(request)
    for n in range(4):
        root = root / (str(n) + "🚀" * 25)
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    with Store(root / ".sidegraph") as store:
        store.add_decision(
            Decision(
                title="Existing memory",
                kind="gotcha",
                context="c",
                choice="c",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
    child = root / "package"
    child.mkdir()
    assert len(str(child).encode("utf-8")) > 400
    with Store(child / ".sidegraph"):
        pass
    for key in ("SIDEGRAPH_DB", "SIDEGRAPH_GRAPH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(child))
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    monkeypatch.chdir(child)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    hooks.session_start()
    raw = capsys.readouterr().out.encode("utf-8")

    _assert_within_limits(raw)
    out = json.loads(raw)
    assert "systemMessage" in out  # the stray-store notice
    assert out["hookSpecificOutput"]["additionalContext"].startswith(
        hooks.STANDING_SEARCH_INSTRUCTION
    )


def _run_early_exit(monkeypatch, capsys, project: Path, **env: str) -> bytes:
    for key in ("SIDEGRAPH_DB", "SIDEGRAPH_DIR", "SIDEGRAPH_GRAPH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    hooks.session_start()
    return capsys.readouterr().out.encode("utf-8")


@posix_long_paths
def test_the_missing_store_guidance_stays_within_limits(monkeypatch, capsys, request):
    """The guidance names the store path four times: a path of about 1,000 bytes of non-ASCII
    took it over the byte budget before it went through the bounded emitter."""
    project = _short_base(request)
    for n in range(9):
        project /= str(n) + "é" * 53
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    assert len(str(project).encode("utf-8")) > 950

    raw = _run_early_exit(monkeypatch, capsys, project, SIDEGRAPH_DB=str(project / ".sidegraph"))

    _assert_within_limits(raw)
    out = json.loads(raw)
    assert list(out) == ["systemMessage"]  # the envelope is unchanged
    assert out["systemMessage"].startswith("Sidegraph: no store at ")
    assert not (project / ".sidegraph").exists()


@posix_long_paths
def test_the_unreadable_store_report_stays_within_limits(monkeypatch, capsys, request):
    base = _short_base(request)
    # Over PATH_MAX on Linux (4,096) as well as macOS (1,024), so lstat fails with
    # ENAMETOOLONG and the store is unreadable rather than missing on both.
    store = base.joinpath(*(str(n) + "é" * 49 for n in range(45)), ".sidegraph")
    assert len(str(store).encode("utf-8")) > 4200

    raw = _run_early_exit(monkeypatch, capsys, base, SIDEGRAPH_DIR=str(store))

    _assert_within_limits(raw)
    out = json.loads(raw)
    assert set(out) == {"systemMessage", "hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_an_early_exit_under_the_limits_is_printed_unchanged():
    output = {"systemMessage": "notice"}
    assert hooks._bounded_output("notice", None) == json.dumps(output)
    output = {
        "systemMessage": "notice",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "line"},
    }
    assert hooks._bounded_output("notice", "line") == json.dumps(output)


def _sections(*pairs: tuple[str, list[str]]) -> list[MapSection]:
    return [MapSection(h, tuple(MapEntry(t) for t in texts)) for h, texts in pairs]


def test_map_under_budget_is_unchanged():
    sections = _sections(("## Unratified proposals", ["- a", "- b"]))
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "head\n\n" + render_sections(sections) + "\n\nstatus",
        }
    }
    assert hooks._fit_session_output("head\n\n", sections, ["status"], []) == json.dumps(output)


def test_domains_are_not_in_the_map_trim_order():
    assert ("## Domains", "domain", "list_domains") not in hooks._MAP_TRIM_ORDER


def test_unratified_proposals_are_trimmed_before_global_mistakes():
    mistakes = [f"- [gotcha] keep rule {i} {'x' * 380}" for i in range(20)]
    sections = _sections(
        ("## Global mistakes & constraints", mistakes),
        (
            "## Unratified proposals",
            [f"- [adr] [unratified] proposal {i} {'y' * 760}" for i in range(10)],
        ),
    )
    status = ["Sidegraph: 10 record(s) awaiting ratification"]
    printed = hooks._fit_session_output("head\n\n", sections, status, ["notice"])
    out = json.loads(printed)
    assert out["systemMessage"] == "notice"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert all(line in ctx for line in mistakes)
    assert "[unratified] proposal 9 " not in ctx
    assert "awaiting ratification" in ctx
    assert "unratified proposals (list_proposed)" in ctx.splitlines()[-1]


def test_cached_map_with_unicode_global_mistakes_fits_the_byte_cap(tmp_path, monkeypatch, capsys):
    from sidegraph.retrieval import TOC_CACHE_KEY, build_toc, render_toc
    from sidegraph.schema import DecisionKind, Domain, Provenance, Scope
    from sidegraph.store import Store
    from tests.store_record_support import _decision

    db = tmp_path / "s.db"
    with Store(db) as store:
        domain = store.add_domain(
            Domain(
                slug="payments",
                title="Payments",
                summary="Settlement.",
                provenance=Provenance(source="manual"),
            )
        )
        store.ratify_domains(accept=[domain.domain_id])
        for i in range(10):
            store.add_decision(
                _decision(
                    f"Global rule {i}",
                    kind=DecisionKind.GOTCHA,
                    scope=Scope.GLOBAL,
                    choice="決" * 240,
                )
            )
        cache = build_toc(store)
        store.set_meta(TOC_CACHE_KEY, json.dumps(cache))

    raw = _hook(monkeypatch, capsys, db, tmp_path / "no-graph.json")

    _assert_within_limits(raw)
    full = render_toc(cache)
    assert len(json.dumps(full).encode()) > hooks.SESSION_START_OUTPUT_BUDGET
    ctx = _context(raw)
    assert len(ctx) < hooks.HOOK_CONTEXT_MAX_CHARS
    assert len(raw) > hooks.SESSION_START_OUTPUT_BUDGET - 1500
    assert ctx.startswith(hooks.STANDING_SEARCH_INSTRUCTION)
    assert hooks.version_line() in ctx
    assert "## Global mistakes & constraints" in ctx
    kept = [line for line in ctx.splitlines() if line.startswith("- [gotcha]")]
    full_entries = [line for line in full.splitlines() if line.startswith("- [gotcha]")]
    assert 0 < len(kept) < len(full_entries) == 10
    assert kept == full_entries[: len(kept)]
    assert 'retrieve_decisions(kind=["gotcha", "constraint", "lesson"])' in ctx.splitlines()[-1]


def test_protected_lines_over_the_limits_drop_the_map_then_clip_then_cut():
    sections = _sections(("## Unratified proposals", ["- a", "- b"]))
    status = ["s" * 12_000]
    notices = ["n" * 20_000]
    printed = hooks._fit_session_output("head\n\n", sections, status, notices)
    out = json.loads(printed)
    assert len(printed.encode("utf-8")) + 1 <= hooks.SESSION_START_OUTPUT_BUDGET
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert len(out["systemMessage"]) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert "## Unratified proposals" not in ctx
    assert ctx.split("\n")[-1] == (
        "Sidegraph: 2 map entries omitted to fit the hook output limit: "
        "2 unratified proposals (list_proposed)."
    )
    assert "s" * 299 + "…" in ctx


def test_no_omission_line_when_nothing_was_omitted():
    printed = hooks._fit_session_output("head\n\n", [], ["é" * 4000], [], budget=4096)
    ctx = json.loads(printed)["hookSpecificOutput"]["additionalContext"]
    assert len(printed.encode("utf-8")) + 1 <= 4096
    assert "omitted" not in ctx


def test_a_non_ascii_context_is_cut_whole_characters_when_nothing_else_helps():
    printed = hooks._fit_session_output("决" * 5000, [], [], [], budget=4096)
    ctx = json.loads(printed)["hookSpecificOutput"]["additionalContext"]
    assert len(printed.encode("utf-8")) + 1 <= 4096
    assert ctx.endswith("…") and set(ctx[:-1]) == {"决"}


def test_fallback_sections_trim_communities_facts_decisions_then_mistakes():
    """Equal-sized entries, so nothing is put back: the order alone decides."""
    row = "x" * 90
    sections = _sections(
        ("## Communities", [f"- c{i} {row}" for i in range(8)]),
        ("## Global mistakes & constraints", [f"- m{i} {row}" for i in range(10)]),
        ("## Recorded decisions", [f"- d{i} {row}" for i in range(10)]),
        ("## Known facts", [f"- f{i} {row}" for i in range(10)]),
    )
    full = len(render_sections(sections))
    printed = hooks._fit_session_output("", sections, [], [], max_chars=full - 1500)
    ctx = json.loads(printed)["hookSpecificOutput"]["additionalContext"]
    assert "- c" not in ctx and "- f" not in ctx
    assert ctx.count("- m") == 10
    kept = [line[3] for line in ctx.split("\n") if line.startswith("- d")]
    assert 0 < len(kept) < 10 and kept == [str(i) for i in range(len(kept))]
    assert ctx.split("\n")[-1].startswith(
        f"Sidegraph: {18 + 10 - len(kept)} map entries omitted to fit the hook output limit: "
        f"8 communities, 10 facts (list_facts), {10 - len(kept)} decision"
    )
