import contextlib
import io
import json

from sidegraph.cli import init_main
from sidegraph.host.claude_settings import RATIFY_POLICY_DEFAULT, RATIFY_POLICY_ENV_VAR
from sidegraph.store import Store


class _FakeTTY(io.StringIO):
    """A stdin double that reports itself as a TTY (a plain io.StringIO's ``isatty()`` is
    always False) -- lets a test drive `sidegraph-init`'s interactive prompt without an
    actual terminal, the same way the suite fakes stdin elsewhere (`monkeypatch.setattr`
    on `sys.stdin`), just with `isatty()` overridden too."""

    def isatty(self) -> bool:
        return True


def test_init_creates_store_and_reports_missing_graph(tmp_path, capsys):
    db = tmp_path / ".sidegraph" / "decisions.db"
    graph = tmp_path / "graphify-out" / "graph.json"
    assert init_main(["--db", str(db), "--graph", str(graph)]) == 0
    assert db.exists()
    out = capsys.readouterr().out
    assert "created store" in out
    assert str(db) in out
    assert "missing graph" in out
    assert "graphify update ." in out


def test_init_reports_found_graph(tmp_path, capsys):
    db = tmp_path / ".sidegraph" / "decisions.db"
    graph = tmp_path / "graphify-out" / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    assert init_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "found graph" in out
    assert str(graph) in out


def test_init_prints_wiring_snippets(tmp_path, capsys):
    # Deliberately NOT named "decisions.db" -- the assertions below check the wiring
    # snippets don't mention the legacy pair, and a --db value ending in that name would
    # make the (unrelated) "created store: <path>" line collide with that substring check.
    db = tmp_path / "custom-store"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    out = capsys.readouterr().out
    # Plugin path leads (installs the MCP server + all three hooks automatically) --
    # no JSON blobs to copy-paste anymore.
    assert "/plugin marketplace add SantyagoSeaman/sidegraph" in out
    assert "/plugin install sidegraph@sidegraph" in out
    # The no-plugin alternative is a single native command, project-scoped.
    assert "claude mcp add sidegraph -s project" in out
    assert "uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main" in out
    assert "sidegraph-mcp" in out
    # Manual hook-by-hook setup lives in the docs, not in the init dump.
    assert "docs/getting-started/claude-code-setup.md" in out
    assert "mcpServers" not in out  # the old .mcp.json blob is gone
    # SIDEGRAPH_DIR is the primary knob; the legacy SIDEGRAPH_DB pair must not appear.
    assert "SIDEGRAPH_DIR=.sidegraph" in out
    assert "SIDEGRAPH_DB" not in out
    assert ".sidegraph/decisions.db" not in out


def test_init_is_idempotent(tmp_path, capsys):
    db = tmp_path / ".sidegraph" / "decisions.db"
    graph = tmp_path / "no.json"
    assert init_main(["--db", str(db), "--graph", str(graph)]) == 0
    capsys.readouterr()
    assert init_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "already initialized" in out
    assert str(db) in out


def test_init_default_db_is_sidegraph_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert init_main([]) == 0
    # ".sidegraph" IS the store now (canonical file-per-record layout), not a bare-file
    # "decisions.db" sitting inside it -- the format marker is the layout's signature.
    assert (tmp_path / ".sidegraph" / "format").is_file()
    assert (tmp_path / ".sidegraph" / "decisions").is_dir()


def test_init_help_documents_default_db_difference(capsys):
    with contextlib.suppress(SystemExit):
        init_main(["--help"])
    out = capsys.readouterr().out
    assert ".sidegraph" in out
    assert "SIDEGRAPH_DIR" in out
    assert "SIDEGRAPH_DB" in out  # back-compat still documented


def test_init_store_open_failure_exits_nonzero(tmp_path, capsys):
    import sqlite3

    db = tmp_path / "bad.db"
    Store(db)  # create a valid store first
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db (git-native store) — corrupt THAT file, not `db` itself.
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 1
    assert "not writable" in capsys.readouterr().out


# -- deferred --db default resolution (review Minor 4) -------------------------------


def _reset_deprecation_warning(monkeypatch):
    import sidegraph.config as config

    monkeypatch.setattr(config, "_deprecation_warned", False)


def test_init_explicit_db_with_sidegraph_db_set_emits_no_deprecation_note(
    tmp_path, capsys, monkeypatch
):
    """The old `default=resolve_store_path(warn_on_create=False)` was evaluated at
    argparse.add_argument() time -- unconditionally, even when --db was about to be given
    explicitly -- so SIDEGRAPH_DB's one-line deprecation note fired regardless. Resolution
    must be deferred until AFTER parse_args, and only when --db was actually omitted."""
    _reset_deprecation_warning(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "legacy-elsewhere.db"))
    explicit_db = tmp_path / "explicit-store"
    assert init_main(["--db", str(explicit_db), "--graph", str(tmp_path / "no.json")]) == 0
    captured = capsys.readouterr()
    assert "SIDEGRAPH_DB is deprecated" not in captured.err
    assert not (tmp_path / "legacy-elsewhere.db").exists()  # SIDEGRAPH_DB never even touched


def test_init_help_emits_no_deprecation_note_even_with_sidegraph_db_set(capsys, monkeypatch):
    _reset_deprecation_warning(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", "some/legacy.db")
    with contextlib.suppress(SystemExit):
        init_main(["--help"])
    captured = capsys.readouterr()
    assert "SIDEGRAPH_DB is deprecated" not in captured.err


def test_init_bare_run_with_sidegraph_db_set_still_emits_deprecation_note_once(
    tmp_path, capsys, monkeypatch
):
    """The deferral must not silently drop the note for the case where it SHOULD fire --
    a bare `sidegraph-init` (no --db) with only the deprecated var set."""
    _reset_deprecation_warning(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "legacy.db"))
    assert init_main(["--graph", str(tmp_path / "no.json")]) == 0
    captured = capsys.readouterr()
    assert "SIDEGRAPH_DB is deprecated" in captured.err


# -- honest "already initialized" for an un-migrated legacy directory (review Minor 5) --


def test_looks_already_initialized_detects_unmigrated_legacy_directory(tmp_path):
    from sidegraph.cli import _looks_already_initialized

    db_dir = tmp_path / "legacy-store"
    db_dir.mkdir()
    (db_dir / "decisions.db").write_text("not a real sqlite file; presence alone matters")
    assert _looks_already_initialized(db_dir) is True


def test_init_reports_already_initialized_for_unmigrated_legacy_directory(tmp_path, capsys):
    """A directory holding an un-migrated legacy decisions.db (no format marker yet --
    migration hasn't run) is a REAL pre-existing store: Store(...) migrates it in place a
    moment later. init_main must say "already initialized", not "created store" -- the
    latter would misreport real, pre-existing decisions as freshly created."""
    import sqlite3

    db_dir = tmp_path / "legacy-store"
    db_dir.mkdir()
    legacy_file = db_dir / "decisions.db"
    conn = sqlite3.connect(str(legacy_file))
    conn.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, "
        "data TEXT NOT NULL);"
        "CREATE TABLE decisions (id TEXT PRIMARY KEY, status TEXT NOT NULL, supersedes TEXT, "
        "data TEXT NOT NULL);"
        "CREATE TABLE anchor_bindings (decision_id TEXT NOT NULL, entity_id TEXT NOT NULL, "
        "data TEXT NOT NULL, PRIMARY KEY (decision_id, entity_id));"
    )
    conn.execute("INSERT INTO meta VALUES ('schema_version', '0.3.0')")
    conn.commit()
    conn.close()

    assert init_main(["--db", str(db_dir), "--graph", str(tmp_path / "no.json")]) == 0
    captured = capsys.readouterr()
    assert "already initialized" in captured.out
    assert "created store" not in captured.out
    assert "migrated" in captured.err  # Store's own migration notice still fires
    assert (db_dir / "format").is_file()  # migration actually ran


# -- Task 1: init and SIDEGRAPH_RATIFY_POLICY in .claude/settings.json ---------------
#
# The owner rejected a silent write (self-certification risk is unmeasured; see
# whitepaper claim C-075) in favor of: ask interactively, with auto-ratification as the
# default answer; write nothing and ask nothing outside a TTY; flags bypass the prompt
# either way. `_FakeTTY` (top of file) simulates an interactive stdin without a real
# terminal.


def _settings_data(tmp_path):
    return json.loads((tmp_path / ".claude" / "settings.json").read_text())


def test_init_interactive_prompt_accepted_by_bare_enter_writes_auto_low_risk(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("\n"))
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: RATIFY_POLICY_DEFAULT}}
    out = capsys.readouterr().out
    assert "Auto-ratify low-risk records?" in out
    assert "Enable it? [Y/n]" in out
    assert (
        f"wrote .claude/settings.json: env.{RATIFY_POLICY_ENV_VAR}={RATIFY_POLICY_DEFAULT}" in out
    )


def test_init_interactive_prompt_accepted_by_yes_writes_auto_low_risk(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("yes\n"))
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: RATIFY_POLICY_DEFAULT}}


def test_init_interactive_prompt_declined_writes_manual(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("no\n"))
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: "manual"}}
    out = capsys.readouterr().out
    assert f"wrote .claude/settings.json: env.{RATIFY_POLICY_ENV_VAR}=manual" in out
    assert f"export {RATIFY_POLICY_ENV_VAR}=manual" in out


def test_init_interactive_unrecognized_input_reasks_once_then_falls_back_to_default(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("banana\nbanana\n"))
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    # Fell back to the default (yes) after two unrecognized answers -- never a third ask.
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: RATIFY_POLICY_DEFAULT}}
    out = capsys.readouterr().out
    assert out.count("Enable it? [Y/n]") == 2


def test_init_interactive_unrecognized_input_then_recognized_answer_is_honored(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("banana\nno\n"))
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: "manual"}}


def test_init_interactive_skips_the_prompt_when_a_policy_is_already_set(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("\n"))  # would answer if asked; must not be
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "manual"}}) + "\n")
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: "manual"}}
    out = capsys.readouterr().out
    assert "Auto-ratify low-risk records?" not in out
    assert "already sets" in out
    assert f"{RATIFY_POLICY_ENV_VAR}=manual" in out


def test_init_non_interactive_writes_nothing_and_asks_nothing(tmp_path, capsys, monkeypatch):
    """No TTY (CI, a script, an agent-driven session): a silent write with nobody to
    answer is exactly what the owner rejected -- the settings file must not be touched or
    even created, and no prompt (input()) may be attempted."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a TTY, and EOF if ever read
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert not (tmp_path / ".claude" / "settings.json").exists()
    out = capsys.readouterr().out
    assert "Auto-ratify low-risk records?" not in out
    assert RATIFY_POLICY_ENV_VAR in out
    assert RATIFY_POLICY_DEFAULT in out
    assert db.exists()


def test_init_non_interactive_never_touches_unparseable_settings_file(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    original = "{not valid json"
    settings_path.write_text(original)
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert settings_path.read_text() == original
    out = capsys.readouterr().out
    assert RATIFY_POLICY_ENV_VAR in out
    assert RATIFY_POLICY_DEFAULT in out
    assert db.exists()  # a settings problem must never be fatal to init


def test_init_ratify_policy_flag_bypasses_the_prompt(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("\n"))  # would answer if asked; must not be
    db = tmp_path / ".sidegraph"
    assert (
        init_main(
            ["--db", str(db), "--graph", str(tmp_path / "no.json"), "--ratify-policy", "manual"]
        )
        == 0
    )
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: "manual"}}
    out = capsys.readouterr().out
    assert "Auto-ratify low-risk records?" not in out
    assert f"wrote .claude/settings.json: env.{RATIFY_POLICY_ENV_VAR}=manual" in out


def test_init_ratify_policy_flag_never_overwrites_an_existing_value(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "auto-all"}}) + "\n")
    db = tmp_path / ".sidegraph"
    assert (
        init_main(
            [
                "--db",
                str(db),
                "--graph",
                str(tmp_path / "no.json"),
                "--ratify-policy",
                "auto-low-risk",
            ]
        )
        == 0
    )
    assert _settings_data(tmp_path) == {"env": {RATIFY_POLICY_ENV_VAR: "auto-all"}}


_RESTART_HINT = "restart your Claude Code session"


def test_init_written_policy_tells_the_user_to_restart_the_session(tmp_path, capsys, monkeypatch):
    """A session started before init already has its MCP server running, and the server
    reads the policy from its own environment, fixed at start: a freshly written value
    reaches it only after a restart, so the write says so."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".sidegraph"
    assert (
        init_main(
            [
                "--db",
                str(db),
                "--graph",
                str(tmp_path / "no.json"),
                "--ratify-policy",
                "auto-low-risk",
            ]
        )
        == 0
    )
    assert _RESTART_HINT in capsys.readouterr().out


def test_init_interactive_answer_tells_the_user_to_restart_the_session(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("\n"))
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json")]) == 0
    assert _RESTART_HINT in capsys.readouterr().out


def test_init_existing_policy_prints_no_restart_hint(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "auto-all"}}) + "\n")
    db = tmp_path / ".sidegraph"
    assert (
        init_main(
            ["--db", str(db), "--graph", str(tmp_path / "no.json"), "--ratify-policy", "manual"]
        )
        == 0
    )
    assert _RESTART_HINT not in capsys.readouterr().out


def test_init_unwritable_settings_file_prints_no_restart_hint(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not json\n")
    db = tmp_path / ".sidegraph"
    assert (
        init_main(
            ["--db", str(db), "--graph", str(tmp_path / "no.json"), "--ratify-policy", "manual"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "isn't valid JSON" in out
    assert _RESTART_HINT not in out


def test_init_no_settings_flag_skips_the_write_and_the_prompt(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _FakeTTY("\n"))  # would answer if asked; must not be
    db = tmp_path / ".sidegraph"
    assert init_main(["--db", str(db), "--graph", str(tmp_path / "no.json"), "--no-settings"]) == 0
    assert not (tmp_path / ".claude" / "settings.json").exists()
    out = capsys.readouterr().out
    assert "Auto-ratify low-risk records?" not in out
    assert RATIFY_POLICY_ENV_VAR in out


def test_init_ratify_policy_and_no_settings_are_mutually_exclusive(capsys):
    with contextlib.suppress(SystemExit):
        init_main(["--no-settings", "--ratify-policy", "manual"])
    err = capsys.readouterr().err
    assert "not allowed with argument" in err


def test_init_ratify_policy_flag_rejects_an_unknown_value(capsys):
    with contextlib.suppress(SystemExit):
        init_main(["--ratify-policy", "sometimes"])
    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_init_help_documents_settings_flags(capsys):
    with contextlib.suppress(SystemExit):
        init_main(["--help"])
    out = capsys.readouterr().out
    assert "--no-settings" in out
    assert "--ratify-policy" in out
