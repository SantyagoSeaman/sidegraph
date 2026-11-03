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


def _domains(count: int) -> list[dict]:
    return [
        {
            "slug": f"domain-{i}",
            "title": f"Платёжный домен №{i} — 決済",
            "summary": f"Обрабатывает расчёты и возвраты, область {i}; 🚀 émoji and café.",
            "parent_slug": None,
            "mistakes": 1 + i % 4,
            "subdomains": 0,
        }
        for i in range(count)
    ]


def _ascii_domains(count: int, **extra) -> list[dict]:
    return [
        {"slug": f"a{i}", "title": f"area {i}", "summary": f"handles part {i}", "mistakes": 1}
        | extra
        for i in range(count)
    ]


def _store_with_cache(tmp_path, cache: dict):
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    store.set_meta(TOC_CACHE_KEY, json.dumps(cache))
    return store, db


def _hook(monkeypatch, capsys, db, graph) -> bytes:
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    return capsys.readouterr().out.encode("utf-8")


def _run_hook(tmp_path, monkeypatch, capsys, cache: dict) -> bytes:
    store, db = _store_with_cache(tmp_path, cache)
    store.close()
    return _hook(monkeypatch, capsys, db, tmp_path / "no-graph.json")  # reader=None


def _context(raw: bytes) -> str:
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]


def _assert_within_limits(raw: bytes) -> None:
    out = json.loads(raw)
    assert len(raw) <= hooks.SESSION_START_OUTPUT_BUDGET < MUSE_CAP
    context = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert len(context) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert len(out.get("systemMessage", "")) <= hooks.HOOK_CONTEXT_MAX_CHARS


def test_large_domain_map_is_trimmed_under_the_budget(tmp_path, monkeypatch, capsys):
    from sidegraph.retrieval import render_toc

    mistake = "- [gotcha] Never write into the engine's graph.json (id: 01KX8NNGFZB8NCQ9N6X8ME7RFS)"
    cache = {"domains": _domains(400), "global_mistakes": [mistake]}
    full = render_toc(cache)
    assert len(json.dumps(full).encode("utf-8")) > MUSE_CAP  # the untrimmed map is over the cap

    # Global mistakes are read live over the cache; serve this one as the live store would.
    monkeypatch.setattr(
        "sidegraph.retrieval.live_global_mistakes",
        lambda _store: {"global_mistakes": [mistake], "unratified": []},
    )
    raw = _run_hook(tmp_path, monkeypatch, capsys, cache)

    assert len(raw) < MUSE_CAP
    _assert_within_limits(raw)
    # The byte cap binds here, not the character cap: the trimmed line fills the byte budget
    # while the context string stays far under the character cap.
    assert len(raw) > hooks.SESSION_START_OUTPUT_BUDGET - 1024
    ctx = _context(raw)
    assert len(ctx) < hooks.HOOK_CONTEXT_MAX_CHARS // 2
    assert ctx.startswith(hooks.STANDING_SEARCH_INSTRUCTION)
    assert hooks.version_line() in ctx
    assert mistake in ctx  # global mistakes outrank the domain tail
    kept = [line for line in ctx.split("\n") if line.startswith("- Платёжный домен")]
    assert kept, "the head of the domain list survives"
    full_lines = set(full.split("\n"))
    assert all(line in full_lines for line in kept)  # never cut mid-line
    # The tail goes, the head stays: kept lines are exactly the first N domains.
    assert [line.split("№")[1].split(" ")[0] for line in kept] == [str(i) for i in range(len(kept))]
    last = ctx.rstrip("\n").split("\n")[-1]
    omitted = 400 - len(kept)
    assert omitted > 1
    assert last == (
        f"Sidegraph: {omitted} map entries omitted to fit the hook output limit: "
        f"{omitted} domains (list_domains)."
    )


def test_ascii_domain_map_is_trimmed_under_the_character_cap(tmp_path, monkeypatch, capsys):
    """Claude Code caps a hook's ``additionalContext`` at 10,000 characters: an ASCII map under
    the byte budget can still be over it, and must be trimmed to the character cap."""
    from sidegraph.retrieval import render_toc

    cache = {"domains": _ascii_domains(240), "global_mistakes": []}
    full = render_toc(cache)
    assert len(full) > 10_000  # over Claude Code's cap...
    assert len(json.dumps(full).encode("utf-8")) < hooks.SESSION_START_OUTPUT_BUDGET - 1024

    raw = _run_hook(tmp_path, monkeypatch, capsys, cache)

    ctx = _context(raw)
    assert len(ctx) <= hooks.HOOK_CONTEXT_MAX_CHARS < 10_000
    assert len(ctx) > hooks.HOOK_CONTEXT_MAX_CHARS - 1024  # the character cap is what bound
    kept = [line for line in ctx.split("\n") if line.startswith("- area ")]
    assert kept and all(line in set(full.split("\n")) for line in kept)
    last = ctx.split("\n")[-1]
    assert last.startswith(f"Sidegraph: {240 - len(kept)} map entries omitted")
    assert "list_domains" in last


def test_a_heading_in_an_early_domain_summary_opens_no_section(tmp_path, monkeypatch, capsys):
    """A summary with ``\\n## Implementation notes`` stays on its domain's line: it neither
    shows as a heading nor shields the domains after it from the trim."""
    early = {
        "slug": "0-payments",
        "title": "Payments",
        "summary": "Handles payments.\n## Implementation notes\nPayment boundaries.",
        "mistakes": 1,
    }
    cache = {"domains": [early, *_ascii_domains(400)], "global_mistakes": []}

    raw = _run_hook(tmp_path, monkeypatch, capsys, cache)

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert "\n## Implementation notes" not in ctx
    assert "- Payments — Handles payments. ## Implementation notes Payment boundaries." in ctx
    kept = sum(1 for line in ctx.split("\n") if line.startswith("- area "))
    assert kept < 400


def test_a_newline_in_a_domain_title_stays_on_its_line(tmp_path, monkeypatch, capsys):
    titled = {"slug": "x", "title": "Payments\n## Notes", "summary": "Money.", "mistakes": 2}
    cache = {"domains": [titled, *_ascii_domains(300)], "global_mistakes": []}

    raw = _run_hook(tmp_path, monkeypatch, capsys, cache)

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert "\n## Notes" not in ctx
    assert "- Payments ## Notes — Money. (2 mistake(s))" in ctx


def test_domains_drop_subdomains_then_quiet_domains_then_the_tail(tmp_path, monkeypatch, capsys):
    """Inside the domain list: subdomains go first, then domains with no mistakes, then the
    rest from the tail."""
    plain = _ascii_domains(300)
    quiet = [
        d | {"slug": f"q{i}", "title": f"quiet {i}", "mistakes": 0} for i, d in enumerate(plain)
    ]
    subs = [
        d | {"slug": f"s{i}", "title": f"sub {i}", "parent_slug": "a0"} for i, d in enumerate(plain)
    ]
    domains = [x for trio in zip(plain, quiet, subs, strict=True) for x in trio]
    cache = {"domains": domains, "global_mistakes": []}

    raw = _run_hook(tmp_path, monkeypatch, capsys, cache)

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert "- sub " not in ctx
    assert "- quiet " not in ctx
    kept = [line for line in ctx.split("\n") if line.startswith("- area ")]
    assert 0 < len(kept) < 300
    assert [line.split(" ")[2] for line in kept] == [str(i) for i in range(len(kept))]


def test_one_oversized_entry_does_not_empty_its_section(tmp_path, monkeypatch, capsys):
    big = {"slug": "big", "title": "huge " * 2400, "summary": "x", "mistakes": 1}
    cache = {"domains": [big, *_ascii_domains(100)], "global_mistakes": []}

    raw = _run_hook(tmp_path, monkeypatch, capsys, cache)

    _assert_within_limits(raw)
    ctx = _context(raw)
    assert sum(1 for line in ctx.split("\n") if line.startswith("- area ")) == 100
    assert (
        "Sidegraph: 1 map entry omitted to fit the hook output limit: 1 domain (list_domains)."
        in ctx.split("\n")
    )


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
    sections = _sections(("## Domains", ["- a", "- b"]))
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "head\n\n" + render_sections(sections) + "\n\nstatus",
        }
    }
    assert hooks._fit_session_output("head\n\n", sections, ["status"], []) == json.dumps(output)


def test_unratified_proposals_go_before_domains_and_mistakes_stay():
    sections = _sections(
        ("## Domains", [f"- domain {i} — ünïcode summary {'x' * 60}" for i in range(30)]),
        ("## Global mistakes & constraints", ["- [gotcha] keep me"]),
        (
            "## Unratified proposals",
            [f"- [adr] [unratified] proposal {i} {'y' * 60}" for i in range(30)],
        ),
    )
    status = ["Sidegraph: 30 record(s) awaiting ratification"]
    budget = len(json.dumps(render_sections(sections)).encode("utf-8")) // 2
    printed = hooks._fit_session_output("head\n\n", sections, status, ["notice"], budget)
    assert len(printed.encode("utf-8")) + 1 <= budget
    out = json.loads(printed)
    assert out["systemMessage"] == "notice"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[unratified] proposal" not in ctx and "## Unratified proposals" not in ctx
    assert "- [gotcha] keep me" in ctx
    assert "awaiting ratification" in ctx
    assert "- domain 0 " in ctx
    last = ctx.split("\n")[-1]
    assert "30 unratified proposals (list_proposed)" in last and "list_domains" in last


def test_protected_lines_over_the_limits_drop_the_map_then_clip_then_cut():
    sections = _sections(("## Domains", ["- a", "- b"]))
    status = ["s" * 12_000]
    notices = ["n" * 20_000]
    printed = hooks._fit_session_output("head\n\n", sections, status, notices)
    out = json.loads(printed)
    assert len(printed.encode("utf-8")) + 1 <= hooks.SESSION_START_OUTPUT_BUDGET
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert len(out["systemMessage"]) <= hooks.HOOK_CONTEXT_MAX_CHARS
    assert "## Domains" not in ctx
    assert ctx.split("\n")[-1] == (
        "Sidegraph: 2 map entries omitted to fit the hook output limit: 2 domains (list_domains)."
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
