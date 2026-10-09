"""Claude Code hook entry points (Stages 4–5, M6).

These are invoked by Claude Code, receive the hook payload on stdin as JSON, and emit their
result on stdout (see the Claude Code hooks docs). Kept here — in the host seam — so the
portable core never reaches into host specifics.

- ``session_start`` (Stage 4): inject task-aware context — the phase-1 top-tier map plus the
  budget-bounded merge of engine subgraph + valid decisions, **mistakes ranked first**.
- ``stop`` (Stage 5): guarded block-to-distill — nudge the agent, at most once per session and
  only once the session has produced >= ``_MIN_USER_PROMPTS`` real user prompts, to propose
  durable decisions via ``propose_decisions`` before the session ends. ``SIDEGRAPH_CAPTURE_
  NUDGE=off`` disables it entirely.
- ``pre_tool_use`` (M6, FR8.3): deliver the records anchored to a file when an agent
  reads or edits it (Read, Grep, Edit, Write, and the Bash read commands ``sed``, ``grep``,
  ``rg``, ``cat``) — a non-blocking ``additionalContext`` block (never denies/blocks the tool
  call), once per file per agent, at most ten files per agent and three per call. On an
  ``Agent`` (or ``Task``) call it instead appends the records for the files the brief names to
  the brief itself, through ``updatedInput``.

All three are wired as of M6.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

# Defined in config.py, not here: server.py (core) needs this constant too (Task 4), and
# core importing from host/ would invert this project's seam rule (host may depend on the
# core, never the reverse). Re-exported so `hooks.TELEMETRY_SESSION_KEY` still resolves for
# callers and tests that reach it through this module.
from ..config import (
    TELEMETRY_SESSION_GROUP_KEY,
    TELEMETRY_SESSION_KEY,
    StoreLocation,
    _store_project_root,
    ancestor_store,
)
from ..store_layout import MEMORY_GUARD_LINE, MISTAKE_KINDS, clip_line

if TYPE_CHECKING:
    from ..hot_index import HotIndex, Record
    from ..integrity import Check, Problem, RunResult

CAPTURE_NUDGE = (
    "Sidegraph: if this session produced a durable decision, lesson, or gotcha — or a "
    "hard-won fact (a benchmark result, an external limit, something learned by trial; "
    "attach it to its decision draft's `facts` list, or pass standalone ones via the "
    "`facts` parameter), capture it now via propose_decisions (draft fields in the tool "
    "description; propose_domains for a recurring unnamed area). If this session made an "
    "existing recorded decision false or too broad, supersede it via supersede_decision "
    "(ids come from get_task_context or the propose result's neighbors). Otherwise just "
    "finish — silence is fine."
)

# Standing SessionStart instruction (owner-approved design, 2026-07-08): tells the agent,
# up front and unconditionally, to use the decision memory before ANY search -- bash
# grep/rg/find, MCP structure-query tools, everything -- not just the files a Read, Edit or Bash
# read command names (the PreToolUse delivery in `pre_tool_use` below covers those and fires once
# per file per agent; this is the behavioral instruction that's meant to cover the rest by
# telling the agent, not by gating a specific tool call). Prepended at the hook-assembly level
# (here, in `session_start`) rather than inside `retrieval.render_toc`/`top_tier_map`
# themselves, so both renderers stay pure content-formatters and this line is written/tested
# in exactly one place regardless of which of the two produced the rest of the text.
# The call it names must be one the tool accepts: `get_task_context` takes `files` and
# `entities` and forbids extra properties, so an earlier wording (`seeds`) produced calls
# that failed validation. The check-plan clause is conditional because the manual installs
# (the Codex hooks recipe, the uvx route) emit this line with no skill installed; the last
# sentence is for a host that lists a tool by name only until it is loaded.
# see design/superpowers/specs/2026-10-01-session-start-text-design.md (D1)
STANDING_SEARCH_INSTRUCTION = (
    "When you need to find or understand code in this project, call "
    "get_task_context(files=[…]) with repo-relative paths before any grep or file search — "
    "decisions, gotchas and a domain map are indexed here. Before a non-trivial change, run "
    "the sidegraph check-plan skill if it is available. If the tool is listed only by name, "
    "load it first."
)


def version_line() -> str:
    """The SessionStart line that names the running package, ``Sidegraph <version>``: the one
    place a session shows which Sidegraph its hooks and tools run, so a launcher that resolved
    an old install cannot stay unseen. Read at call time, from the package.
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D1)
    """
    from .. import __version__

    return f"Sidegraph {__version__}"


# D7.2 (staleness-machinery wave, E7 observation): a user's own project/personal
# settings.json can register a SECOND SessionStart hook alongside the plugin's, firing
# session_start() twice for one logical session — a double map injection. ONE bounded
# meta key (never a PER-SESSION key: meta rows are not pruned in general, so a per-session
# key grows forever — fact `01KYFYMB0…` in this store is the reason the *value*, never
# key-absence, carries the guard; only the PreToolUse key prefixes are expired, by
# `prune_meta_prefixes` at SessionStart) holding ``<session_id>|<iso-timestamp>``. Precedent for
# hooks writing meta: the pre_tool_use per-file ledger (`_PRETOOL_FILE_KEY_PREFIX` below)
# and `TELEMETRY_SESSION_KEY` (config.py).
_SESSION_START_KEY = "session_start"
_SESSION_START_DEDUPE_SECONDS = 60


def _session_identity(payload: dict) -> tuple[str | None, str | None]:
    """The session this payload belongs to, and the workspace session above it.

    ``payload["session_id"]`` does not mean the same thing on both hosts, and reading it
    directly attributed a Codex store's whole history to one session. Measured 2026-09-19:

    - Claude Code: per session, and the transcript is named after it — all 74 session ids in
      this repository's own store are exactly transcript stems.
    - Codex: the *umbrella* workspace session. It spans days, survives ``resume``, and covers
      every thread beneath it — 1928 of 1929 events in a second live store landed in one
      bucket across 27 hours. The thread's own identity is its rollout file; Codex's
      ``session-start.command.input`` schema carries no thread id and no ``agent_type``.

    So the transcript path is the one field that identifies a session on both hosts, and its
    stem is the id on the host where the two agree. Returns ``(key, group)``: ``group`` is the
    host's own ``session_id`` when it differs from ``key``, else ``None`` — nothing extra to
    record on a host whose session id already IS the key.

    Beyond attribution, this is what unblocks the 60s double-injection dedupe
    (:func:`_session_start_duplicate`): two Codex threads opened within a minute reported the
    same id, so the second one's context map was suppressed as a duplicate. Observed in the
    live rollouts, which start in pairs seconds apart.
    """
    raw_id = payload.get("session_id")
    session_id = raw_id if isinstance(raw_id, str) and raw_id else None
    raw_path = payload.get("transcript_path")
    stem = None
    if isinstance(raw_path, str) and raw_path:
        # PurePosixPath, not Path: the stem of a host-written path, never touched on disk.
        stem = PurePosixPath(raw_path).stem or None
    key = stem or session_id
    group = session_id if session_id and session_id != key else None
    return key, group


def _agent_identity(payload: dict) -> str | None:
    """The subagent this payload belongs to, or ``None`` for the session's own agent.

    Claude Code gives a subagent's hook payload its PARENT's ``session_id`` and
    ``transcript_path`` and its own ``agent_id`` (a ``claude -p`` probe on 2.1.287 showed it
    for Explore, Plan and general-purpose subagents). State keyed on the session alone
    therefore covers the whole agent tree: one agent's one-shot nudge spent it for all the
    others.

    Only ``agent_id`` counts, never ``agent_type``: a main session started with
    ``claude --agent reader`` carries ``agent_type`` and no ``agent_id``, and two subagents of
    one type are two agents. Codex wires no PreToolUse hook here, and whether it fills these
    fields is unmeasured; its schema does declare them as optional.
    see design/superpowers/specs/2026-10-01-per-agent-hook-state-design.md (D1)
    """
    raw = payload.get("agent_id")
    return raw if isinstance(raw, str) and raw else None


def _store_holds_records(path: Path) -> bool:
    """True when any file sits under ``decisions/``, ``facts/`` or ``domains/`` of the store
    at ``path``: a few ``listdir`` calls, no ``Store`` opened (opening would create things).
    False for a path that is not a directory or cannot be listed — the caller stays silent
    on what it cannot see."""
    for sub in ("decisions", "facts", "domains"):
        try:
            if any(name.endswith(".json") for name in os.listdir(path / sub)):
                return True
        except OSError:
            continue
    return False


def _stray_store_line(location: StoreLocation) -> str | None:
    """The SessionStart line that names an empty store a subdirectory launch created, when the
    repository's own store with records sits above it; ``None`` otherwise.

    "Existing at the anchor wins" (``config.resolve_store_location``) keeps a store an older
    Sidegraph created at ``<subdir>/.sidegraph`` for good, so the fix has to be reported, not
    chosen: a per-package store that was just initialised is empty too (spec R5). The walk is
    ``config.ancestor_store`` from the store's own anchor, the one the lookup would have made
    had the stray store not been there. It never applies to an absolute store, an explicit
    path or the legacy ``SIDEGRAPH_DB``, which the lookup never touches.
    see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md (D4)
    """
    if location.base is None:
        return None
    if not os.environ.get("SIDEGRAPH_DIR") and os.environ.get("SIDEGRAPH_DB"):
        return None
    value = os.path.relpath(location.path, location.base)
    if value.startswith(os.pardir):
        return None
    if _store_holds_records(Path(location.path)):
        return None
    above = ancestor_store(location.base, value)
    if above is None or not _store_holds_records(Path(above.path)):
        return None
    return (
        f"Sidegraph: this session uses an empty store at {location.path}; {above.path} holds "
        f"this repository's memory. An older Sidegraph may have created {location.path} for a "
        f"session started in a subdirectory: remove {location.path} to use {above.path}."
    )


# `version-skew`: the plugin manifest sits under the plugin root Claude Code sets for its hooks
# (`CLAUDE_PLUGIN_ROOT`) or Codex does (both, in fact; `PLUGIN_ROOT` is a generic name another
# tool could set, which is why the manifest's own name is checked). Claude's cache holds the
# `.claude-plugin` manifest, Codex's both.
# see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D2)
_PLUGIN_ROOT_VARS = ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")
_MANIFEST_PATHS = (
    Path(".claude-plugin") / "plugin.json",
    Path(".codex-plugin") / "plugin.json",
)


def _plugin_manifest_version() -> str | None:
    """The ``version`` of the Sidegraph plugin manifest under the plugin root, or ``None`` when
    there is no root, no manifest, a manifest that is not JSON or not named ``sidegraph``, or one
    without a string version. The first manifest that exists is the one read.
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D2)
    """
    root = next((os.environ[name] for name in _PLUGIN_ROOT_VARS if os.environ.get(name)), None)
    if root is None:
        return None
    manifest = next((m for m in (Path(root) / rel for rel in _MANIFEST_PATHS) if m.is_file()), None)
    if manifest is None:
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("name") != "sidegraph":
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


def _numeric_version(text: str) -> tuple[int, ...] | None:
    """The dotted numeric release of ``text`` (``0.10.0``), ignoring a local label (``+local``)
    and anything after the numbers, with trailing zeros dropped so ``0.9`` equals ``0.9.0``;
    ``None`` when it does not start with a number. A string comparison would put ``0.10.0``
    below ``0.9.0``.
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D2)"""
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.split("+", 1)[0].strip())
    if match is None:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


_PACKAGE_NEWER = (
    "Sidegraph: the package is {package} but the plugin is {plugin}: the installed plugin copy "
    "is stale. Update the plugin from its marketplace."
)
_PLUGIN_NEWER = (
    "Sidegraph: the plugin is {plugin} but the package is {package}: this session runs an old "
    "launcher cache or a pinned install. Restart the session so SessionStart re-resolves "
    "`@main`, or remove the pinned install (for example `uv tool uninstall sidegraph`)."
)

# `plugin-off-in-subdirectories`: case (a) names the file to move the enabling out of, case (b)
# the first nested directory that leaves the plugin off. The fix changes the person's settings, so
# the model line ends by telling the model to say so and not to edit them (as `refresh-hook-missing`
# does). The human's notice is sent only with (b): a nested `enabledPlugins` is evidence that
# someone launches there, whereas (a) alone is what every collaborator of a project-scoped install
# has, and the fix is per person.
_PLUGIN_OFF_TELL_THE_USER = "Tell the user, and do not change their settings unasked."
_PLUGIN_OFF_AT_ROOT_ONLY = (
    "Claude sessions started below the repository root run without Sidegraph: it is enabled only "
    "in `.claude/settings.json`. Enable it in `.claude/settings.local.json` at the root (it "
    "applies to every directory) or in your user settings."
)
_PLUGIN_OFF_NESTED = (
    "Sidegraph is off in {count} {directories} with Claude settings of their own, the first "
    "being `{first}`: sessions started there run without it. Enable it there, or at the "
    "repository root in `.claude/settings.local.json` (it applies to every directory) or in "
    "your user settings."
)


def host_checks(location: StoreLocation) -> tuple[Check, ...]:
    """The integrity checks that need the host: ``stray-store``, a closure over this launch's
    ``StoreLocation`` because the portable registry's ``Inputs`` stays host-free, and the two
    host-wiring checks, ``version-skew`` and ``plugin-off-in-subdirectories``. The hook passes
    ``CHECKS[:5] + host_checks(location) + CHECKS[5:]`` to keep the line order, and
    ``sidegraph-doctor`` passes them to ``doctor.curate``, so ``doctor.py`` imports nothing
    from this seam.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D1, D4 check 5)
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D2, D3)
    """
    from .. import __version__
    from ..integrity import Check, Inputs, Problem, _NotRun
    from . import claude_settings

    def detect_stray_store(inputs: Inputs) -> Problem | None:
        line = _stray_store_line(location)
        if line is None:
            return None
        return Problem(
            check="stray-store",
            severity="degraded",
            summary="empty store beside the launch directory",
            fix=f"remove {location.path}",
            line=line,
            notice=line,
        )

    def detect_version_skew(inputs: Inputs) -> Problem | None:
        plugin = _plugin_manifest_version()
        if plugin is None:
            raise _NotRun
        package_release, plugin_release = _numeric_version(__version__), _numeric_version(plugin)
        if package_release is None or plugin_release is None:
            raise _NotRun
        if package_release == plugin_release:
            return None
        template = _PACKAGE_NEWER if package_release > plugin_release else _PLUGIN_NEWER
        line = template.format(package=__version__, plugin=plugin)
        return Problem(
            check="version-skew",
            severity="advisory",
            summary="package and plugin versions differ",
            fix="update the plugin" if template is _PACKAGE_NEWER else "restart the session",
            line=line,
            notice=line,
        )

    def detect_plugin_off(inputs: Inputs) -> Problem | None:
        reach = claude_settings.plugin_reach(_store_project_root(inputs.store_dir))
        if reach is None:
            raise _NotRun
        findings: list[tuple[str, str]] = []
        if reach.project_only:
            findings.append(
                (
                    str(reach.root / claude_settings.SETTINGS_RELATIVE_PATH),
                    _PLUGIN_OFF_AT_ROOT_ONLY,
                )
            )
        if reach.off:
            count = len(reach.off)
            findings.append(
                (
                    str(reach.off[0]),
                    _PLUGIN_OFF_NESTED.format(
                        count=count,
                        directories="directory" if count == 1 else "directories",
                        first=reach.off[0].relative_to(reach.root).as_posix(),
                    ),
                )
            )
        if not findings:
            return None
        details = " ".join(detail for _path, detail in findings)
        return Problem(
            check="plugin-off-in-subdirectories",
            severity="advisory",
            summary="plugin off in subdirectories",
            fix="enable it in .claude/settings.local.json at the root, or in user settings",
            line=f"{details} {_PLUGIN_OFF_TELL_THE_USER}",
            notice=details if reach.off else None,
            findings=tuple(findings),
        )

    return (
        Check("stray-store", frozenset({"session"}), detect_stray_store),
        Check("version-skew", frozenset({"session"}), detect_version_skew),
        Check(
            "plugin-off-in-subdirectories",
            frozenset({"session", "doctor"}),
            detect_plugin_off,
        ),
    )


# A notice reaches the human at most once a day per check, and again at once when the severity
# rises. One meta key per check id (a bounded set, never per session) holds
# ``<severity>|<iso-timestamp>``.
# see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D5)
_NOTICE_KEY_PREFIX = "integrity_notice:"
_NOTICE_REPEAT = timedelta(hours=24)


def _is_transient_lock(error: BaseException) -> bool:
    """``Store()`` failed because another process holds the index's lock: ``SQLITE_BUSY`` or
    ``SQLITE_LOCKED``. ``sqlite_errorcode`` carries the extended result code (BUSY_SNAPSHOT is
    517), whose low byte is the primary one. A lock is not a broken store, so the hook keeps
    printing ``{}`` for it; a read-only or unreadable index is not transient.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D3)
    """
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _report_unreadable_store(location: StoreLocation, error: BaseException, now: datetime) -> None:
    """Print what a store that cannot open says: the registry runs ``store-unreadable`` alone
    (the other checks read the store or the graph this path does not have), and the human gets
    the notice as ``systemMessage`` while the model is told memory tools will fail. No noise
    control and no dedupe here: both live in the store that did not open, and a broken store is
    worth reporting at every start.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D3)
    """
    from .. import integrity

    inputs = integrity.Inputs(store_dir=Path(location.path), now=now, open_error=error)
    result = integrity.run(inputs, "session", (integrity.STORE_UNREADABLE,))
    if not result.problems:
        print(json.dumps({}))
        return
    problem = result.problems[0]
    print(
        json.dumps(
            {
                "systemMessage": problem.notice,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": problem.line,
                },
            }
        )
    )


def _notice_is_due(stamp: str | None, severity: str, now: datetime) -> bool:
    """A notice is due when its key is absent or unparseable, a day or more has passed since the
    recorded one (either direction: a stamp from the future does not silence a notice for as long
    as it is ahead), or the severity is higher than the recorded one.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D5)
    """
    if stamp is None:
        return True
    from ..integrity import SEVERITY_RANK

    recorded, _, iso = stamp.partition("|")
    before_rank = SEVERITY_RANK.get(recorded)
    try:
        before = datetime.fromisoformat(iso)
        if before_rank is None or before.tzinfo is None:
            return True
        return abs(now - before) >= _NOTICE_REPEAT or SEVERITY_RANK[severity] > before_rank
    except (ValueError, TypeError):
        return True


def _claim_notice(store, problem: Problem, now: datetime) -> bool:
    """True iff this process may send ``problem``'s notice to the human, and the claim is made.

    The decision and the write are one ``Store.update_meta_if`` (an immediate transaction): the
    hook registered twice fires both copies for one session start, with different session ids,
    and only the process that writes the stamp emits. When the claim itself fails the notice is
    sent anyway: a duplicate is better than silence. A failure that is a lock another process
    holds is raised instead, so the caller can stop writing: every further write would wait out
    the busy timeout too (``_due_notices``).
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D5)
    """

    def decide(current: str | None) -> str | None:
        if not _notice_is_due(current, problem.severity, now):
            return None
        return f"{problem.severity}|{now.isoformat()}"

    try:
        return store.update_meta_if(_NOTICE_KEY_PREFIX + problem.check, decide)
    except Exception as e:
        if _is_transient_lock(e):
            raise
        return True


def _recorded_notice_keys(index) -> frozenset[str]:
    """The ``integrity_notice:`` meta keys that exist, read from the hook's read-only index
    connection (before it closes), so that clearing a key is a write only when there is one to
    clear. Empty when the index cannot be read: under a lock, the time to write nothing."""
    if index is None:
        return frozenset()
    try:
        rows = index.execute(
            "SELECT key FROM meta WHERE substr(key, 1, ?) = ?",
            (len(_NOTICE_KEY_PREFIX), _NOTICE_KEY_PREFIX),
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    return frozenset(row[0] for row in rows)


def _due_notices(
    store, result: RunResult, now: datetime, recorded: frozenset[str] | None = None
) -> list[str]:
    """The notices to send to the human now, highest severity first (registry order within one
    severity), and the housekeeping that goes with them: a check that ran and found nothing
    deletes its key, so a problem that comes back after a fix is reported at once. A check that
    did not run (``_NotRun``, an exception, a switch) leaves its key alone.

    Writes are rationed, because each one waits out the busy timeout when another process holds
    the write lock (5 s by default, per write). A key is deleted only when it exists:
    ``recorded`` is the set of existing keys the caller already read (``None`` reads each clean
    id's key from the store, which takes no write lock), so a healthy start writes nothing. And
    the first failure that is a lock stops all further claims and deletes: the remaining due
    notices are sent unclaimed, a duplicate being better than silence.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D5)
    """
    from ..integrity import SEVERITY_RANK

    with_notice = [p for p in result.problems if p.notice]
    with_notice.sort(key=lambda p: -SEVERITY_RANK[p.severity])  # stable: registry order kept
    writable = True
    notices: list[str] = []
    for problem in with_notice:
        claimed = True
        if writable:
            try:
                claimed = _claim_notice(store, problem, now)
            except Exception:  # a lock another process holds: write nothing more
                writable = False
        if claimed and problem.notice:
            notices.append(problem.notice)
    for check_id in sorted(result.clean):
        if not writable:
            break
        key = _NOTICE_KEY_PREFIX + check_id
        try:
            if recorded is None:
                if store.get_meta(key) is None:
                    continue
            elif key not in recorded:
                continue
            store.delete_meta(key)
        except Exception as e:
            if _is_transient_lock(e):
                break
    return notices


def _session_start_duplicate(store, session_id: str, now: datetime) -> bool:
    """True iff ``session_start()`` already ran for ``session_id`` within
    :data:`_SESSION_START_DEDUPE_SECONDS` of ``now`` (design D7.2), in either direction: a
    stamp up to that far AHEAD also counts, because a sibling hook that took a later ``now``
    may have written first. Same session id AND a fresh
    timestamp -> duplicate, ledger left untouched (so a third rapid call still compares
    against the ORIGINAL write, not a duplicate's own arrival time); anything else
    (no prior key, a different session, or an expired window) -> not a duplicate, and the
    fresh marker is written here as the "else write and emit" half of "same session id AND
    timestamp < 60s old -> exit silently; else write and emit". Best-effort: any
    read/parse/write failure (a lock timeout included) is treated as "not a duplicate"
    (never blocks the session).

    Two steps, because the hook registered twice on one host fires both copies for one
    session at once. A read followed by a write is two transactions: both copies read the
    old value, both decide "not a duplicate", both inject the map. So (1) a lock-free read
    answers a duplicate at once, with no write lock to wait for, and (2) anything else is
    decided again INSIDE one immediate transaction (``Store.update_meta_if``), where the
    second copy sees what the first wrote.
    see design/superpowers/specs/2026-10-02-session-start-dedupe-atomic-design.md (D2)

    A NAIVE-but-parseable ``prev_stamp`` (schema requires aware timestamps, but the
    ledger is a bare string a hand edit or an older format could still leave naive) is
    explicitly treated as "not a duplicate" via the ``tzinfo is not None`` guard in
    :func:`_is_recent_stamp` — the same guard ``capture._session_id_fallback`` already
    applies for its own stamp comparison — rather than ever attempting ``now - prev_dt``
    (CORRECTION-6, code review): that subtraction raises ``TypeError`` on a naive/aware
    mismatch, which is NOT a ``ValueError`` the inner ``except`` catches, so it used to
    escape straight to the outer ``except Exception`` and skip the write entirely — the
    ledger got stuck on the bad stamp forever, never self-healing. Falling through to the
    write instead means the very next call is compared against a freshly-written, valid
    stamp."""

    def recent_same_session(raw: str | None) -> bool:
        return raw is not None and _is_recent_stamp(raw, session_id, now)

    def decide(current: str | None) -> str | None:
        # A duplicate found inside the transaction leaves the ledger untouched.
        return None if recent_same_session(current) else f"{session_id}|{now.isoformat()}"

    try:
        if recent_same_session(store.get_meta(_SESSION_START_KEY)):
            return True
        return not store.update_meta_if(_SESSION_START_KEY, decide)
    except Exception:
        return False


def _is_recent_stamp(raw: str, session_id: str, now: datetime) -> bool:
    """True iff the ledger value ``raw`` (``<session_id>|<iso-timestamp>``) names ``session_id``
    with an AWARE timestamp less than :data:`_SESSION_START_DEDUPE_SECONDS` from ``now``."""
    prev_id, _, prev_stamp = raw.partition("|")
    if prev_id != session_id:
        return False
    try:
        prev_dt = datetime.fromisoformat(prev_stamp)
    except ValueError:
        return False
    # abs(): `now` is taken before the read, so a sibling hook that committed a later stamp
    # first leaves a genuine duplicate a few ms "in the future"; a stamp a whole window or
    # more ahead is not a duplicate.
    return (
        prev_dt.tzinfo is not None
        and abs((now - prev_dt).total_seconds()) < _SESSION_START_DEDUPE_SECONDS
    )


# Calibration (owner's verdict from live manual testing): Claude Code calls the Stop hook
# after EVERY completed agent turn, and the once-per-session capture ledger let the very
# first one through -- so the nudge fired at the end of turn one of practically every
# session, before there was anything worth distilling. Requiring >= 2 real user prompts is
# the cheapest signal that a session has moved past a single request/response and might
# plausibly contain a durable decision.
_MIN_USER_PROMPTS = 2


# Host-emitted turns persist as `type: "user"` entries with NO distinguishing flag (measured
# on real session files, 2026-07-30; re-measured 2026-10-03 over the 400 most recent
# transcripts) -- content prefix is the only way to tell them from a typed prompt. The
# slash-command wrappers, a finished background agent's `<task-notification>`, an agent-team
# `<teammate-message` (no closing `>`: it carries attributes) and a `!` command's output.
# A typed slash command that starts `<command-message>` and a `!` command's `<bash-input>`
# are a person's own turn and keep counting. A genuine prompt starting with one of these
# literals would merely raise the nudge threshold by one; this is a heuristic gate, not
# provenance.
_HOST_EMITTED_PREFIXES = (
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<task-notification>",
    "<teammate-message",
    "<bash-stdout>",
    # The lead's own session receives a teammate's message as this plain-text line, with the
    # `<teammate-message` tag on the next line.
    "Another Claude session sent a message",
)

# Newer Claude Code writes provenance on some user lines. These values mark a line the host
# wrote, including task notifications in plain prose that no prefix catches. They are only
# ever a "not a person" signal: teammate, slash-command and `!` lines carry no fields at all,
# so an absent field proves nothing (measured over the 400 most recent transcripts,
# 2026-10-03).
# see design/superpowers/specs/2026-10-03-stop-gate-host-messages-design.md
_HOST_ORIGIN_KINDS = frozenset({"task-notification"})
_HOST_PROMPT_SOURCES = frozenset({"system"})


def _is_real_user_prompt(entry: object) -> bool:
    """True for a transcript line that is an actual user-typed prompt.

    Claude Code session transcripts (JSONL) use ``type: "user"`` for ALL of: a real prompt
    (``message.content`` is a string, or a list containing at least one non-``tool_result``
    block -- e.g. text/image), a tool result being fed back to the agent (a list of ONLY
    ``tool_result`` blocks), hook-injected feedback (top-level ``isMeta: true`` -- 4c-findings
    §2: one real prompt + one injection used to read as exactly ``_MIN_USER_PROMPTS``),
    post-compaction continuation summaries (``isCompactSummary: true``), and slash-command
    wrappers (no flag at all -- excluded by :data:`_HOST_EMITTED_PREFIXES`). All shapes
    measured on persisted session files, 2026-07-30.
    """
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return False
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") in _HOST_ORIGIN_KINDS:
        return False
    if entry.get("promptSource") in _HOST_PROMPT_SOURCES:
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith(_HOST_EMITTED_PREFIXES)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                continue
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].lstrip().startswith(_HOST_EMITTED_PREFIXES)
            ):
                continue
            return True
        return False
    return False


# Codex rollouts (see design/superpowers/specs/2026-10-01-codex-capture-gate-design.md, D1).
# A rollout has no `type: "user"` lines: a user turn is a `response_item` whose payload is a
# `message` with `role: "user"`, and Codex also writes its OWN user-role messages there. A turn
# is classified by the start of its first `input_text` block, and a person's prompt is any turn
# that does not start with one of these. Prefixes, without a closing `>`, because the
# `<hook_prompt …>` and `<codex_internal_context …>` tags carry attributes. As with the Claude
# wrappers above, a genuine prompt that begins with one merely raises the threshold by one; the
# list failing is visible (a new injected tag counts as a prompt), the opposite of an event
# schema changing under a counter that then reads zero. The Claude wrappers are included for
# older Codex builds and for threads that carry them.
_CODEX_INJECTED_PREFIXES = (
    "# AGENTS.md",
    "<environment_context",
    "<recommended_plugins",
    "<skill",
    "<turn_aborted",
    "<user_instructions",
    "<hook_prompt",  # a Stop hook's block reason, fed back: Codex's `isMeta`
    "<codex_internal_context",
    "<send_user_message_question_reply",  # a person's answer to a question, mid-turn
) + _HOST_EMITTED_PREFIXES
# `thread_source` values of a thread no person works in (a subagent, Codex's automatic reviewer).
_CODEX_NON_PERSON_THREADS = frozenset({"subagent", "guardian_review"})
# A headless `codex exec` run: scripts and review panels, where a Stop block would replace the
# run's `-o` output (or an SDK caller's result) with the continuation. Told apart by either
# field: the CLI's `originator`, and the `source` string `exec`, which is also what remains when
# the Codex TypeScript SDK runs `codex exec` under an originator of its own (`codex_sdk_ts`).
# All 149 `codex_exec` rollouts measured carry `source: "exec"`; a person's thread has `cli` or
# `vscode`.
_CODEX_HEADLESS_ORIGINATOR = "codex_exec"
_CODEX_HEADLESS_SOURCE = "exec"


def _is_codex_real_user_prompt(entry: dict) -> bool:
    """True for a rollout line that is a person's prompt: a user-role `message` `response_item`
    whose first `input_text` block does not start with :data:`_CODEX_INJECTED_PREFIXES`.

    The `event_msg` ``item_completed`` lines that mirror each prompt are never counted, so a
    prompt is not counted twice, and a message with no `input_text` block (an image alone)
    does not count. An image prompt (``<image …>``, the image, ``</image>``, then the text)
    counts once, by its first block. see
    design/superpowers/specs/2026-10-01-codex-capture-gate-design.md (D1)
    """
    if entry.get("type") != "response_item":
        return False
    payload = entry.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "message"
        or payload.get("role") != "user"
    ):
        return False
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "input_text"
            and isinstance(block.get("text"), str)
        ):
            return not block["text"].lstrip().startswith(_CODEX_INJECTED_PREFIXES)
    return False


# Substance signal for agentic one-shot sessions (design D1, E10 H-F0): a `claude -p`
# session has exactly one real prompt however much work follows, so the >=2-prompts gate
# never arms there — every stage-session capture in E8/E9/E9b had been armed by the
# isMeta-counting defect 05c9010 fixed. The tool branch is scoped to the host's one-shot
# entrypoint ONLY: unscoped, it armed at the end of turn 1 in 93% of multi-prompt
# interactive sessions (measured over 806 transcripts, spec review F2), burning the
# once-per-session ledger before the session's richer content existed. In an sdk-cli
# one-shot, turn 1 IS the whole session, so those two moments coincide. Threshold 5 is
# the measured must-arm floor (E10 backfill = 5; stage sessions 14–46; trivial 0–2).
_MIN_TOOL_USES = 5
_ONE_SHOT_ENTRYPOINT = "sdk-cli"


class _TranscriptStats(NamedTuple):
    real_prompts: int
    tool_uses: int
    entrypoint: str | None
    # Codex only (the first `session_meta` of a rollout); None / False for a Claude transcript.
    thread_source: str | None = None
    originator: str | None = None
    codex_subagent: bool = False
    source: str | None = None


def _transcript_stats(transcript_path: str) -> _TranscriptStats:
    """One pass over a transcript JSONL file, either host's shape: real user prompts
    (``_is_real_user_prompt``, or ``_is_codex_real_user_prompt`` for a Codex rollout's
    ``response_item`` lines), assistant ``tool_use`` blocks, the first-seen top-level
    ``entrypoint`` field, and, for a rollout, the thread kind its FIRST ``session_meta`` names
    (a forked subagent carries its own meta first and its parent's second).

    A single malformed line is skipped rather than fatal, so one corrupt row doesn't zero
    out an otherwise-substantial session (the skip is shared by all the counts — one
    parse, one policy). A missing/unreadable file (bad path, permissions) propagates to
    the caller -- ``stop``'s outer ``try/except`` turns that into the same "print {} and
    allow" as any other never-crash failure. A transcript with no ``entrypoint`` anywhere
    returns ``None`` — the caller degrades to the prompt-only gate, never to arming.
    see design/superpowers/specs/2026-10-01-codex-capture-gate-design.md (D1)
    """
    real_prompts = 0
    tool_uses = 0
    entrypoint: str | None = None
    meta_seen = False
    thread_source: str | None = None
    originator: str | None = None
    codex_subagent = False
    source_name: str | None = None
    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entrypoint is None:
                ep = entry.get("entrypoint")
                if isinstance(ep, str) and ep:
                    entrypoint = ep
            if not meta_seen and entry.get("type") == "session_meta":
                meta_seen = True
                meta = entry.get("payload")
                if isinstance(meta, dict):
                    ts, orig = meta.get("thread_source"), meta.get("originator")
                    thread_source = ts if isinstance(ts, str) and ts else None
                    originator = orig if isinstance(orig, str) and orig else None
                    source = meta.get("source")
                    codex_subagent = isinstance(source, dict) and "subagent" in source
                    source_name = source if isinstance(source, str) and source else None
            if _is_real_user_prompt(entry) or _is_codex_real_user_prompt(entry):
                real_prompts += 1
            elif entry.get("type") == "assistant":
                message = entry.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), list):
                    tool_uses += sum(
                        1
                        for block in message["content"]
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    )
    return _TranscriptStats(
        real_prompts, tool_uses, entrypoint, thread_source, originator, codex_subagent, source_name
    )


def _is_substantial(stats: _TranscriptStats) -> bool:
    """True when a transcript's stats say the session is worth a capture nudge.

    Claude Code: >= ``_MIN_USER_PROMPTS`` real prompts, or an ``sdk-cli`` one-shot with >=
    ``_MIN_TOOL_USES`` tool uses. Codex: the same prompt count, never the tool branch, and
    never a thread no person works in (a subagent or the automatic reviewer, by
    ``thread_source`` or by a subagent ``source``) or a headless ``codex exec`` run (its
    ``originator`` is ``codex_exec``, or its ``source`` is ``exec``, which also catches the
    TypeScript SDK's own originator), whose Stop block would replace its output. A rollout
    with no ``thread_source`` (an older Codex) is a person's thread. A Claude transcript has
    none of these fields, so the thread rules are inert there.
    see design/superpowers/specs/2026-10-01-codex-capture-gate-design.md (D2)
    """
    if (
        stats.codex_subagent
        or stats.thread_source in _CODEX_NON_PERSON_THREADS
        or stats.originator == _CODEX_HEADLESS_ORIGINATOR
        or stats.source == _CODEX_HEADLESS_SOURCE
    ):
        return False
    return stats.real_prompts >= _MIN_USER_PROMPTS or (
        stats.entrypoint == _ONE_SHOT_ENTRYPOINT and stats.tool_uses >= _MIN_TOOL_USES
    )


def session_start() -> None:
    """SessionStart hook (Stage 4): inject the phase-1 top-tier map as additionalContext.

    Reads $SIDEGRAPH_DIR (or legacy $SIDEGRAPH_DB) / $SIDEGRAPH_GRAPH (same env, same
    resolution, as the server and CLI — see ``config.resolve_store_location``). A relative
    store that does not exist under $CLAUDE_PROJECT_DIR is looked up in the ancestors, inside
    the repository, so a session started in a subdirectory uses the repository's store; the
    graph is the one that store's project holds (``config.default_graph_path``). Must never
    crash the session — any failure prints ``{}`` and exits 0, except a store that cannot be
    opened (not a lock another process holds): that prints a ``systemMessage`` naming the cause
    and the fix.

    The status lines (pending ratification, drift, borrowed or stale graph, stray store, missing
    graph, orphaned records, skipped files) come from the integrity registry, which also yields
    the notices sent to the human as ``systemMessage``, at most once a day per check — see
    ``sidegraph.integrity`` and design/superpowers/specs/2026-10-02-integrity-self-check-design.md.

    Double-injection dedupe (design D7.2): a session id repeated within
    :data:`_SESSION_START_DEDUPE_SECONDS` exits silently, since a user's own hook
    registration can fire this alongside the plugin's for one logical session — see
    :func:`_session_start_duplicate`.

    First of all, before the store opens, it records the commit uv resolved for an ``@main``
    install so the hot-path hooks launch from it, in a ``try`` of its own: whatever happens
    there changes nothing below — see :mod:`sidegraph.host.launch` and
    design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md.
    """
    if len(sys.argv) > 1:
        refuse_arguments(
            "sidegraph-session-start", "Injects the project's decision map at session start."
        )
    with contextlib.suppress(Exception):
        from . import launch

        launch.record_launch_commit()
    try:
        from ..config import default_graph_path, resolve_store_location
        from ..engine.reader import GraphifyReader, open_borrowed_reader
        from ..retrieval import (
            TOC_CACHE_KEY,
            build_toc,
            render_toc,
            top_tier_map,
            unratified_mistakes,
        )
        from ..store import Store

        payload = _read_payload()
        location = resolve_store_location(
            root=os.environ.get("CLAUDE_PROJECT_DIR"), search_ancestors=True
        )
        # A store that cannot open used to print `{}` and switch memory off with no word to
        # anyone. Only a lock another process holds keeps that: it passes by itself.
        # see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D3)
        try:
            store = Store(location.path)
        except Exception as e:
            if _is_transient_lock(e):
                raise
            _report_unreadable_store(location, e, datetime.now(UTC))
            return
        # Not payload["session_id"] directly: that field is the umbrella workspace session
        # on Codex, not this session — see _session_identity.
        session_id, session_group = _session_identity(payload)

        # D7.2: double-injection dedupe, checked BEFORE any other work (sync/TOC/nudges) —
        # a duplicate SessionStart call for the same session within the window exits
        # silently, same "print {} and return" shape the Stop hook's own one-shot ledger
        # uses. Gated on a real session id (see _session_start_duplicate's docstring).
        if session_id and _session_start_duplicate(store, session_id, datetime.now(UTC)):
            print(json.dumps({}))
            return

        # Own try/except, like the ratification nudge below: telemetry must never cost the
        # map, which is this hook's only real deliverable.
        try:
            if session_id:
                # D7.3 (staleness-machinery wave, E8: author=None session=None on
                # Stop-channel captures): this write is now UNCONDITIONAL — no longer
                # gated on telemetry_enabled() — because capture._propose_one falls back
                # to it for provenance.session_id when the caller passed none (best-effort
                # attribution, not security; see that fallback's own docstring).
                store.set_meta(
                    TELEMETRY_SESSION_KEY,
                    f"{session_id}|{datetime.now(UTC).isoformat()}",
                )
                if session_group:
                    store.set_meta(TELEMETRY_SESSION_GROUP_KEY, session_group)
            # Pruning is deliberately NOT gated on telemetry_enabled() (practitioner
            # re-review round 2): it used to be, which meant opting out froze the 30-day
            # retention of whatever journal already existed — an opt-out that makes the
            # data live LONGER is the wrong way round for GDPR and for a works council.
            # Recording stays off when the flag is off; expiry keeps running regardless,
            # so opting out strictly reduces what is retained.
            store.prune_telemetry_events()
        except Exception:
            pass
        # The PreToolUse per-file keys are up to ten per agent (hundreds a day in a session that
        # spawns subagents), so they expire like the journals do; the two one-shot prefixes
        # older versions wrote are cleared too, and so are the Stop hook's per-session re-arm
        # stamps. Its own try: the journal prune above failing must not leave these to pile up,
        # and vice versa.
        # see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D2)
        with contextlib.suppress(Exception):
            store.prune_meta_prefixes(
                (*_PRETOOL_PRUNE_PREFIXES, _CAPTURE_REARM_KEY_PREFIX),
                older_than=timedelta(days=_PRETOOL_KEY_RETENTION_DAYS),
            )

        # A linked worktree has the tracked store and no graph: with none of its own, the
        # main checkout's graph is read (`borrowed_from` is that checkout's root) and synced
        # index-only (`canonical_writes=False`): the worktree's index starts cold, and the sync
        # derives it without rewriting any tracked file (its moved rung abstains).
        # see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2)
        borrowed_from: Path | None = None
        graph_path: Path | None = None
        try:
            graph_path = default_graph_path(store.path)
            reader = GraphifyReader(graph_path)
        except Exception:
            reader = None
            borrowed = open_borrowed_reader(store.path)
            if borrowed is not None:
                reader, borrowed_from = borrowed
        try:
            from .. import sync as _sync

            if borrowed_from is None:
                _sync.maybe_sync(store, reader)
            else:
                _sync.maybe_sync(store, reader, canonical_writes=False)
        except Exception:
            pass  # lazy sync is best-effort; the map still renders un-synced

        # Real TOC (§5): the sync path precomputes named domains into store meta. Read it
        # when present and non-empty; any accepted domain means the mind model has a name
        # to show. Malformed/missing cache or zero accepted domains falls back to today's
        # community-listing map exactly (no accepted domains = no behavior change). The
        # cache is untrusted (a manual edit, or a shape mismatch from a future/older
        # version) — render_toc is called under its own try/except so a shape it can't
        # handle degrades to the legacy map too, rather than escaping to the outer handler
        # and losing store-only content along with it (never-crash AND graceful-degrade).
        text = None
        try:
            cache = json.loads(store.get_meta(TOC_CACHE_KEY) or "null")
        except json.JSONDecodeError:
            cache = None
        if borrowed_from is not None:
            # A worktree's cache is only as fresh as its last sync, and the sync is best-effort:
            # build the domain map in memory instead of trusting a cached one. The count walks
            # every domain's decisions (about 0.1 s on a real store), which is the price of a
            # map that is never older than the store. The borrowed reader is open, so the
            # count includes the decisions anchored to a whole document, like the main
            # checkout's.
            try:
                cache = build_toc(store, reader)
            except Exception:
                cache = None
        if isinstance(cache, dict) and cache.get("domains"):
            try:
                # Domain/initiative summaries remain cache-owned, but unratified global
                # mistakes must reflect the live Store even when the graph is unavailable
                # and lazy sync cannot refresh an older cache that predates this key.
                cache = {**cache, "unratified": unratified_mistakes(store)}
                text = render_toc(cache)
            except Exception:
                text = None
        if text is None:
            text = top_tier_map(store, reader)
        # Standing instruction goes first, ahead of either renderer's content — see
        # STANDING_SEARCH_INSTRUCTION's docstring for why it lives here and not in the
        # renderers. The version line follows it, so a session names the package it runs.
        # see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D1)
        text = f"{STANDING_SEARCH_INSTRUCTION}\n\n{version_line()}\n\n{text}"

        # Drift refresh (drift→supersede D4): it runs UNCONDITIONALLY — it is what keeps the
        # retrieval markers' cache fresh; SIDEGRAPH_DRIFT_NUDGE=off gates the PROSE only (spec I5,
        # option b — gating the refresh froze markers at their last pre-off state with no
        # self-heal path). Its return value is the drift check's input: `None` when the refresh
        # could not run, which the check treats as "not run", never as the old cache's count.
        # Own try/except: a drift failure must never cost the map.
        drift_count: int | None = None
        try:
            from .. import sync as _sync_drift

            drift_count = _sync_drift.refresh_code_drift_cache(store)
        except Exception:
            pass  # the drift line must never cost the map

        # The status lines and the notices for the human: one registry decides what is wrong
        # (pending ratification, drift, borrowed graph, stale graph, stray store, missing
        # graph, orphaned records, skipped files). Its model lines are appended in registry
        # order, as the inline blocks they replace were; its notices go out as `systemMessage`.
        # Own try/except: the map is this hook's only real deliverable.
        # see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D1, D5)
        notices: list[str] = []
        try:
            from .. import integrity
            from ..gitio import open_index_ro

            now = datetime.now(UTC)
            index = open_index_ro(Path(store.path))
            try:
                recorded = _recorded_notice_keys(index)
                result = integrity.run(
                    integrity.Inputs(
                        store_dir=Path(store.path),
                        now=now,
                        store=store,
                        index=index,
                        reader=reader,
                        graph_path=graph_path,
                        borrowed_from=borrowed_from,
                        drift_count=drift_count,
                    ),
                    "session",
                    integrity.CHECKS[:5] + host_checks(location) + integrity.CHECKS[5:],
                )
            finally:
                if index is not None:
                    index.close()
            for problem in result.problems:
                if problem.line:
                    text += f"\n\n{problem.line}"
            notices = _due_notices(store, result, now, recorded)
        except Exception:
            pass  # the status lines must never cost the map

        output: dict = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }
        if notices:
            output = {"systemMessage": "\n".join(notices), **output}
        print(json.dumps(output))
    except Exception:
        print(json.dumps({}))


# A session that was already nudged is nudged again when at least this long has passed since its
# last nudge AND at least this many commits were authored since then. The last nudge is the meta
# key ``capture_rearm:<session>`` (an ISO timestamp; SessionStart expires it with the other
# per-session keys). A session captured before the key existed falls back to the ``captured_at``
# of its ``capture_sessions`` row, which the schema makes NOT NULL.
# see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D2, D3)
_CAPTURE_REARM_KEY_PREFIX = "capture_rearm:"
_REARM_GAP = timedelta(minutes=30)
_REARM_MIN_COMMITS = 10
_REARM_GIT_TIMEOUT_SECONDS = 2
# ``git log --since`` compares committer dates and stops at the first commit older than the
# bound, while the count is by author date. A commit authored after the nudge whose committer
# date is older (clock skew, an explicit GIT_COMMITTER_DATE) would hide everything behind it, so
# the walk starts this much earlier than the nudge. Dropping the bound costs 0.36 s per call on
# a 55,290-commit repository, and the check runs at every Stop of a nudged session.
_REARM_SKEW_MARGIN = timedelta(days=1)


def _parse_stamp(value: str | None) -> datetime | None:
    """An ISO timestamp from the ledger as an aware datetime, or ``None`` when it is absent or
    not one."""
    try:
        stamp = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def _read_capture_ledger(
    peek: sqlite3.Connection, session_id: str
) -> tuple[str, str | None] | None:
    """What the read-only peek knows of a session: ``(captured_at, capture_rearm)`` when it was
    already nudged (the second is ``None`` for a session that predates the key), else ``None``.

    One connection reads both, so the in-gap Stop stays one cheap open.
    see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D5)
    """
    row = peek.execute(
        "SELECT captured_at FROM capture_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    stamp = peek.execute(
        "SELECT value FROM meta WHERE key = ?", (_CAPTURE_REARM_KEY_PREFIX + session_id,)
    ).fetchone()
    return row["captured_at"], (stamp["value"] if stamp else None)


def _commits_since(store_path: Path, since: datetime) -> int | None:
    """How many commits authored at or after ``since`` are reachable from a local branch or
    ``HEAD``, or ``None`` when git could not say (a failure or a timeout means no re-arm).

    Author dates, not committer dates: a rebase or cherry-pick of old commits is not new work.
    ``--since`` only bounds the walk, and it compares committer dates: it is set
    ``_REARM_SKEW_MARGIN`` before ``since`` so a commit authored after ``since`` with an older
    committer date is still reached. The author-time filter is the count. Runs in the project
    the store belongs to, with git's repository-local variables dropped. Stdlib and ``gitenv``
    only: ``verify.find_store_project_repo`` would pull in the models on the hook's cheap path.
    see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D3)
    """
    import subprocess

    from .. import gitenv

    since_epoch = int(since.timestamp())
    walk_epoch = int((since - _REARM_SKEW_MARGIN).timestamp())
    try:
        done = subprocess.run(
            ["git", "log", "--branches", "HEAD", f"--since={walk_epoch}", "--format=%at", "--"],
            cwd=_store_project_root(store_path),
            env=gitenv.git_env(),
            capture_output=True,
            text=True,
            timeout=_REARM_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return sum(1 for line in done.stdout.split() if line.isdigit() and int(line) >= since_epoch)


def _rearm_commits(store_path: Path, captured_at: str, stamp: str | None) -> int | None:
    """The commits that make an already-nudged session due another nudge, or ``None`` when it
    is not due: inside the gap (no git work at all), a git failure, or too few commits.
    see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D2, D5)
    """
    last = _parse_stamp(stamp) or _parse_stamp(captured_at)
    if last is None or datetime.now(UTC) - last < _REARM_GAP:
        return None
    commits = _commits_since(store_path, last)
    return commits if commits is not None and commits >= _REARM_MIN_COMMITS else None


def stop() -> None:
    """Stop hook (Stage 5): guarded block-to-distill.

    Fires the distillation nudge once the session looks substantial: >=
    ``_MIN_USER_PROMPTS`` real prompts, OR — one-shot (``sdk-cli``) sessions only —
    >= ``_MIN_TOOL_USES`` assistant tool calls (see ``_transcript_stats``, ``_is_substantial``
    and the scoping rationale above ``_MIN_TOOL_USES``). A session that was already nudged
    (capture ledger + ``stop_hook_active``) is nudged again only after at least
    ``_REARM_GAP`` and ``_REARM_MIN_COMMITS`` new commits (``_rearm_commits``), and exactly
    one of several parallel Stops wins that nudge (a compare-and-swap on ``capture_rearm``).
    It reads a Claude Code transcript and a Codex rollout alike; on Codex only a person's
    interactive thread can arm. Disable entirely with ``SIDEGRAPH_CAPTURE_NUDGE=off``. Must
    never crash the session — any failure allows the stop.
    see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D2-D5)

    Ordering matters: the substance gate runs BEFORE ``mark_captured`` is ever written, and
    a gate failure returns without touching the ledger at all. A session gated at turn 1 (not
    yet substantial) is therefore still eligible to nudge later once it becomes substantial —
    marking it early would have permanently suppressed that later nudge. A captured session
    skips the gate and the transcript: it passed the gate once, and the re-arm rests on
    commits.
    """
    if len(sys.argv) > 1:
        refuse_arguments(
            "sidegraph-stop",
            "Nudges the agent to record what the session learned, once it has done enough.",
        )
    try:
        if os.environ.get("SIDEGRAPH_CAPTURE_NUDGE") == "off":
            print(json.dumps({}))
            return

        from ..config import resolve_store_location
        from ..gitio import open_index_ro

        payload = _read_payload()
        if payload.get("stop_hook_active"):
            print(json.dumps({}))
            return
        session_id, _ = _session_identity(payload)
        if not session_id:
            print(json.dumps({}))
            return

        # Read-only peek at the capture ledger BEFORE the transcript parse: every Stop after
        # the capturing one would otherwise re-read the whole transcript just to learn the
        # session is done. Any failure (no index, a lock, an empty index.db) falls through
        # to the authoritative `was_captured` check below; the peek never writes. The same
        # connection reads the session's `capture_rearm` stamp, so a Stop inside the gap exits
        # here, and git runs only after the peek is closed.
        store_path = Path(
            resolve_store_location(
                root=os.environ.get("CLAUDE_PROJECT_DIR"),
                warn_on_create=False,
                search_ancestors=True,
            ).path
        )
        peek = open_index_ro(store_path)
        ledger: tuple[str, str | None] | None = None
        if peek is not None:
            try:
                ledger = _read_capture_ledger(peek, session_id)
            except sqlite3.Error:
                pass
            finally:
                peek.close()
        rearm_commits: int | None = None
        if ledger is not None:
            rearm_commits = _rearm_commits(store_path, *ledger)
            if rearm_commits is None:
                print(json.dumps({}))
                return

        if ledger is None:
            transcript_path = payload.get("transcript_path")
            # Must be a non-empty str, not just truthy: a malformed/adversarial payload with an
            # int here (e.g. `1`) would otherwise reach `open()`, which treats an int as a raw
            # file descriptor -- `1` is stdout -- and closes it on the `with` block's exit,
            # taking the hook's own ability to print its result down with it.
            stats = (
                _transcript_stats(transcript_path)
                if isinstance(transcript_path, str) and transcript_path
                else _TranscriptStats(0, 0, None)
            )
            if not _is_substantial(stats):
                print(json.dumps({}))  # not substantial yet -- do NOT mark_captured
                return

        # Imported here, below both early exits, the peek and the substance gate: the models cost
        # tens of milliseconds and the common Stop never reaches this point.
        # see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D6)
        from ..store import Store

        store = Store(
            resolve_store_location(
                root=os.environ.get("CLAUDE_PROJECT_DIR"), search_ancestors=True
            ).path
        )
        rearm_key = _CAPTURE_REARM_KEY_PREFIX + session_id
        if ledger is None:
            if store.was_captured(session_id):
                print(json.dumps({}))
                return
        else:
            # Compare-and-swap: the new stamp goes in only if the stored one is still what the
            # peek read (`None` for a session that predates the key), so two parallel Stops
            # past the threshold cannot both nudge. It is taken before the drift refresh so the
            # losing Stop does none of that work.
            expected = ledger[1]
            stamp = datetime.now(UTC).isoformat()
            if not store.update_meta_if(
                rearm_key, lambda current: stamp if current == expected else None
            ):
                print(json.dumps({}))
                return

        # Drift clause (drift→supersede D5): refresh AFTER the substance gate and ledger
        # check (no git work on trivial sessions), unconditionally — same I5 option-b rule
        # as SessionStart, the kill-switch gates the prose only. Stop must REFRESH rather
        # than read SessionStart's cache: the measured mechanism is same-cycle (capture at
        # specify-time, implement commits later in the SAME session), so at Stop the
        # drifted set is precisely what SessionStart could not yet see. Own try/except:
        # a refresh failure omits the clause, never the nudge.
        reason = CAPTURE_NUDGE
        try:
            from .. import sync as _sync_drift

            n = _sync_drift.refresh_code_drift_cache(store)
            if n and os.environ.get("SIDEGRAPH_DRIFT_NUDGE") != "off":
                # 601 + 183 = 784 at n=5 on a first nudge, inside the staleness-wave's pinned
                # <=800 anti-creep bound. A re-armed nudge adds the commit sentence below (43
                # characters), which is why test_host_stop.py's bound is 850.
                reason += (
                    f" Also: {n} drifted record(s) — their anchored code changed after "
                    "capture; if this session's work overtook any of them, "
                    "supersede_decision it (ids: get_task_context or sidegraph-doctor)."
                )
        except Exception:
            pass

        if rearm_commits is None:
            store.mark_captured(session_id)  # before emitting: a crash cannot double-nudge
            # The stamp the re-arm counts from. A failed write only costs the fallback to
            # `captured_at`, never the nudge.
            with contextlib.suppress(Exception):
                store.set_meta(rearm_key, datetime.now(UTC).isoformat())
        else:
            reason += f" Since the last capture prompt: {rearm_commits} commits."
        print(json.dumps({"decision": "block", "reason": reason, "suppressOutput": True}))
    except Exception:
        print(json.dumps({}))


# The tools whose calls deliver records (exact-match, inside the hook: `hooks.json`'s matcher is
# user-editable, so trusting it alone would deliver for any tool in a hand-wired setup). Bash is
# wired for four read commands only (the manifests' `if` entries); the hook still checks the line.
# see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D3)
_POINT_OF_READ_TOOLS = frozenset({"Read", "Grep", "Edit", "Write", "Bash"})

# One claim per file per agent in the meta table, keyed
# ``pretool_file:<session>:<agent or ->:<repo-relative path>`` with the claim's ISO timestamp as
# the value (SessionStart expires it). ``-`` stands for the session's own agent: without it that
# agent's key prefix would be the session alone, which every subagent's keys also start with, and
# the subagents' claims would spend the main agent's cap. Deliberately NOT the Stop hook's
# `capture_sessions` table: that table means "already nudged to *distill*", a different guard.
# see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D2)
_PRETOOL_FILE_KEY_PREFIX = "pretool_file:"

# The two one-shot prefixes older versions wrote. Nothing writes them now; SessionStart still
# clears what is left of them.
_PRETOOL_LEGACY_KEY_PREFIXES = ("pretool_nudge:", "pretool_nudge_path:")
_PRETOOL_PRUNE_PREFIXES = (*_PRETOOL_LEGACY_KEY_PREFIXES, _PRETOOL_FILE_KEY_PREFIX)

# SessionStart expires a per-file key this many days after its claim. The value is the claim's
# ISO timestamp (the older one-shot keys held "1", which SessionStart also expires).
_PRETOOL_KEY_RETENTION_DAYS = 30

# At most this many files are delivered to one agent, and at most this many per call. The cap is
# exact (one SQL statement counts and inserts); the per-call bound is applied before claiming, so
# the several processes Claude Code spawns for one Bash line cannot add up past it.
_FILES_PER_AGENT = 10
_FILES_PER_CALL = 3

# How much of a record the block carries: the title and the first sentence of the choice, each
# clipped at a word boundary. Two records, or three when the first two are both mistakes.
_RECORD_TITLE_CHARS = 90
_RECORD_CHOICE_CHARS = 120
_RECORDS_SHOWN = 2
_RECORDS_SHOWN_WHEN_TWO_MISTAKES = 3

# Recording is a different mechanism from delivering records and needs a different tool set: a
# touch is evidence that the agent worked on a file, and a Bash read is not one (D8). Kept as its
# own frozenset because `hooks.json`'s matcher is user-editable — trusting the matcher alone would
# record arbitrary tools in a hand-wired setup, the same belt-and-braces reasoning
# `_POINT_OF_READ_TOOLS` already applies.
_TOUCH_TOOLS = frozenset({"Read", "Grep", "Edit", "Write"})

# The tools whose input is a subagent's brief (``Task`` is the older name). Compared exactly:
# ``TaskCreate`` and the other ``Task*`` tools carry no brief.
# see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1)
_AGENT_TOOLS = frozenset({"Agent", "Task"})

# The block the Agent branch appends to a brief, and what bounds it: per file two records (a
# brief covers several files, so not the Read path's two-or-three), then at most six record lines
# and 3,000 characters for the whole block, whole files dropped from the end.
# see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1, D3)
_BRIEF_OPEN = "--- Recorded decisions for the files in this task (added by Sidegraph) ---"
_BRIEF_CLOSE = "--- end of Sidegraph records ---"
_BRIEF_RECORDS_PER_FILE = 2
_BRIEF_MAX_LINES = 6
_BRIEF_MAX_CHARS = 3000


def _touch_path(tool_input: object, root: str) -> str | None:
    """The repo-relative path a touch event should carry, or None when nothing can join.

    ``os.path.relpath`` is purely lexical, so BOTH sides are ``realpath``-resolved first:
    when the root is reached through a symlink (macOS ``/tmp`` -> ``/private/tmp``, a
    symlinked workspace) a lexical relpath returns a ``..``-prefixed path for every touch,
    all of them get dropped, and D9's swallow hides the silence — zero signal, zero errors.
    Returns None for a pattern-only Grep, a path outside the root, and a directory: anchors
    are files, and recording a key that can never join is worse than recording nothing.
    """
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        base = os.path.realpath(root)
        target = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
        rel = os.path.relpath(target, base)
    except (OSError, ValueError):
        return None
    if rel == os.curdir or rel.startswith(os.pardir):
        return None
    if os.path.isdir(target):
        return None
    return rel


def _touch_root(location: StoreLocation) -> str:
    """The directory touch paths are made relative to: the base the store's relative value was
    anchored to, after the ancestor lookup. A launch in ``R/sub`` that finds ``R/.sidegraph``
    roots at ``R``, so a touch reads ``sub/a.py`` and joins the repo-relative anchors; a nested
    ``.config/sidegraph`` at ``R`` still roots at ``R``, not at the store's parent. An absolute
    store has no base: ``$CLAUDE_PROJECT_DIR``, else the cwd.
    see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md (D2)
    """
    return location.base or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


class _HotOpener:
    """Opens the hot index at most once per hook run, however many steps ask for it.

    Touch recording and the nudge both need the index and both resolve the same store, so the
    second step reuses the first one's handle (or its refusal: ``None`` is remembered too, so a
    store that cannot be used is looked at once). A handle whose write hit the busy timeout
    (``HotIndex.write_failed``) is not handed out again: the next write would wait out the same
    timeout, so a Read under a held lock costs one wait, not two. ``close`` releases the handle
    at the end of the run.
    see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D3)
    """

    def __init__(self) -> None:
        self._asked = False
        self._index: HotIndex | None = None

    def __call__(self, store_path: str | os.PathLike[str]) -> HotIndex | None:
        if not self._asked:
            from ..hot_index import HotIndex

            self._asked = True
            self._index = HotIndex.open(store_path)
        if self._index is not None and self._index.write_failed:
            return None
        return self._index

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None


def _record_touch_event(payload: dict, hot: _HotOpener) -> None:
    """Record one touch. Runs FIRST in ``pre_tool_use``, before every delivery gate (D4).

    Delivery's early exits belong to delivery: inheriting them would record nothing after an
    agent's first read of a file, nothing in a store without memory yet, and nothing when
    delivery is switched off. The single gate shared with it is ``session_id``, because a touch
    that cannot be attributed cannot be written at all.
    Never raises (D9) — this runs inside a PreToolUse hook, where an exception would
    interfere with the user's own tool call.

    The row goes through the hot index (``hot_index.HotIndex``), not ``Store``: no package import,
    no digest walk, and no store created as a side effect of a tool call. A project with no usable
    index records nothing.
    see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D3)
    """
    try:
        from ..config import resolve_store_location, telemetry_enabled

        if not telemetry_enabled():
            return
        tool = payload.get("tool_name")
        if tool not in _TOUCH_TOOLS:
            return
        session_id, _ = _session_identity(payload)
        if not session_id:
            return
        agent_id = _agent_identity(payload)
        # warn_on_create=False: recording runs on every matching tool call regardless of
        # delivery's own gates (D4), including SIDEGRAPH_GREP_NUDGE=off, and the location is only
        # looked up here, never created: printing "creating new store" on a path the user
        # explicitly silenced and cannot act on is noise. Resolved before the path is made
        # because the touch root depends on where the store was found (spec D2).
        location = resolve_store_location(
            root=os.environ.get("CLAUDE_PROJECT_DIR"), warn_on_create=False, search_ancestors=True
        )
        path = _touch_path(payload.get("tool_input"), _touch_root(location))
        if path is None:
            return
        index = hot(location.path)
        if index is None:
            return
        index.record_touch(session_id, path, str(tool), agent=agent_id)
    except Exception:
        return


def _records_for_path(store, rel_path: str) -> list[tuple]:
    """The live decisions anchored to ``rel_path``, each with whether it is a proposal, in the
    order a reader should meet them: accepted mistakes, accepted rest, proposed last, newest
    first within each bucket. The reference ``HotIndex.records_for`` must equal, byte for byte,
    on raw index rows (``tests/test_hot_index.py``); the hook itself never builds a ``Store``.

    Mistakes-first is the product's one hard ranking guarantee (``MISTAKE_KINDS``, imported,
    never re-spelled). WITHIN each bucket the order is newest-first: the scan order is ULID mint
    order, so without this a cap would spend itself on the OLDEST records of a busy path.

    Pure read. A row that fails to parse is skipped individually.
    see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D1, D6)
    """
    from ..retrieval import partition_by_trust

    mistakes: list = []
    rest: list = []
    seen: set[str] = set()
    try:
        entities = list(store.iter_concrete_entities())
    except Exception:
        return []
    for e in entities:
        try:
            if e.descriptor is None or e.descriptor.file_path != rel_path:
                continue
            for d in store.valid_decisions_for_entity(e.entity_id):
                if d.id in seen:
                    continue
                seen.add(d.id)
                (mistakes if d.kind.value in MISTAKE_KINDS else rest).append(d)
        except Exception:
            continue
    accepted_mistakes, proposed_mistakes = partition_by_trust(mistakes)
    accepted_rest, proposed_rest = partition_by_trust(rest)
    ordered = sorted(accepted_mistakes, key=lambda d: d.valid_from, reverse=True)
    ordered += sorted(accepted_rest, key=lambda d: d.valid_from, reverse=True)
    proposals = sorted(
        [*proposed_mistakes, *proposed_rest], key=lambda d: d.valid_from, reverse=True
    )
    return [(d, False) for d in ordered] + [(d, True) for d in proposals]


def _titles_for_path(store, rel_path: str) -> list[str]:
    """The titles of :func:`_records_for_path`, clipped as the hook clips them and tagged
    ``[unratified]`` when proposed: the part of the block that a test of a regulated mode reads."""
    return [
        clip_line(" ".join(d.title.split()), _RECORD_TITLE_CHARS)
        + (" [unratified]" if proposed else "")
        for d, proposed in _records_for_path(store, rel_path)
    ]


# A full stop after one of these does not end a sentence: "(e.g. the rows)" would otherwise
# leave "…(e.g." as the whole gist. Matched case-insensitively, as a whole word.
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.")


def _ends_in_abbreviation(text: str, end: int) -> bool:
    """Whether ``text[: end + 1]`` ends in one of :data:`_ABBREVIATIONS` as a whole word."""
    head = text[: end + 1].lower()
    for abbreviation in _ABBREVIATIONS:
        if head.endswith(abbreviation):
            before = head[: -len(abbreviation)]
            if not before or not before[-1].isalnum():
                return True
    return False


def _first_sentence(text: str) -> str:
    """``text`` with its whitespace collapsed, up to and including the first sentence end: a
    ``.``, ``!`` or ``?`` followed by a space, except a full stop that closes an abbreviation
    (:data:`_ABBREVIATIONS`). All of it when there is none."""
    collapsed = " ".join(text.split())
    for i in range(len(collapsed) - 1):
        if collapsed[i] in ".!?" and collapsed[i + 1] == " ":
            if collapsed[i] == "." and _ends_in_abbreviation(collapsed, i):
                continue
            return collapsed[: i + 1]
    return collapsed


def _clip_choice(sentence: str, limit: int) -> str:
    """:func:`clip_line` of ``sentence``, without the opening backtick of an inline code span the
    clip cut in two: a lone backtick would read as the start of code that never ends. A sentence
    whose own backticks do not pair is left alone."""
    clipped = clip_line(sentence, limit)
    if clipped != sentence and clipped.count("`") % 2 == 1 and sentence.count("`") % 2 == 0:
        dangling = clipped.rfind("`")
        clipped = clipped[:dangling] + clipped[dangling + 1 :]
    return clipped


def _record_line(record: Record) -> str:
    decision = record.decision
    title = clip_line(" ".join(decision["title"].split()), _RECORD_TITLE_CHARS)
    if record.proposed:
        title += " [unratified]"
    choice = _clip_choice(_first_sentence(decision["choice"]), _RECORD_CHOICE_CHARS)
    gist = f" — {choice}" if choice else ""
    return f"- [{decision['kind']}] {title}{gist} (id {decision['id']})"


def _records_block(rel: str, records: Sequence[Record]) -> str:
    """The block for one file: a header, the top two records (three when the first two are both
    mistakes), and a line naming the call that returns the rest when there is a rest.

    The order is the one :meth:`HotIndex.records_for` returns, which is the model layer's
    per-file order and not ``get_task_context(files=[f])``'s (that orders per entity), so the
    last line promises the remaining records and not the same sequence.
    see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D1)
    """
    top_two_are_mistakes = len(records) >= 2 and all(
        r.decision["kind"] in MISTAKE_KINDS for r in records[:2]
    )
    count = _RECORDS_SHOWN_WHEN_TWO_MISTAKES if top_two_are_mistakes else _RECORDS_SHOWN
    shown = records[:count]
    lines = [f"Recorded for {rel} ({len(shown)} of {len(records)}, mistakes first):"]
    lines += [_record_line(r) for r in shown]
    if len(records) > len(shown):
        files = json.dumps([rel], ensure_ascii=False)
        lines.append(f"More: get_task_context(files={files}).")
    return "\n".join(lines)


def _files_named(tool: object, payload: dict, tool_input: dict, root: str) -> list[str]:
    """The repo-relative files this call reads or edits, in the order the call names them.

    Read, Edit, Write and Grep name one (``_touch_path``: a directory, a pattern-only Grep and a
    path outside the root name none). A Bash call names the regular files its ``sed``, ``grep``,
    ``rg`` and ``cat`` commands read (``bash_paths.read_paths``), resolved from the payload's
    ``cwd``.
    """
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return []
        from ..bash_paths import read_paths

        cwd = payload.get("cwd")
        return read_paths(command, cwd if isinstance(cwd, str) and cwd else root, root)
    rel = _touch_path(tool_input, root)
    return [rel] if rel else []


def _delivery(payload: dict, hot: _HotOpener) -> str | None:
    """The text :func:`pre_tool_use` hands the agent for this call, or ``None`` for nothing.

    The order is the one the three delivery rules need: the files are taken first (no index is
    opened for a call that names none), then ONE scan of the entities finds the anchored ones,
    the first three that have records are chosen (the same three in every process of one Bash
    line), and only then does each claim go through, so a file another process or an earlier
    call already delivered, or an agent already at its cap, costs nothing and prints nothing.
    see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D2, D3, D5)
    """
    if os.environ.get("SIDEGRAPH_GREP_NUDGE") == "off":
        return None
    tool = payload.get("tool_name")
    if tool not in _POINT_OF_READ_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    session_id, _ = _session_identity(payload)
    if not session_id:
        return None

    from ..config import resolve_store_location

    # warn_on_create=False: a tool call never creates a store (D3), so the "creating new
    # store" notice would announce something that is not going to happen.
    location = resolve_store_location(
        root=os.environ.get("CLAUDE_PROJECT_DIR"), warn_on_create=False, search_ancestors=True
    )
    files = _files_named(tool, payload, tool_input, _touch_root(location))
    if not files:
        return None
    index = hot(location.path)
    if index is None:
        return None

    anchored = index.anchored_entities(files)
    chosen: list[tuple[str, list[Record]]] = []
    for rel in dict.fromkeys(files):
        if rel not in anchored:
            continue
        records = index.records_for(anchored[rel])
        if records:
            chosen.append((rel, records))
            if len(chosen) == _FILES_PER_CALL:
                break

    prefix = f"{_PRETOOL_FILE_KEY_PREFIX}{session_id}:{_agent_identity(payload) or '-'}:"
    now = datetime.now(UTC).isoformat()
    blocks: list[str] = []
    for rel, records in chosen:
        # Claim BEFORE emitting, in one statement that also counts: a crash cannot deliver a
        # file twice, two parallel reads of one agent cannot both deliver it, and the agent's
        # eleventh file is refused.
        try:
            if index.claim_file(prefix, rel, now, _FILES_PER_AGENT):
                blocks.append(_records_block(rel, records))
        except sqlite3.OperationalError:
            break  # the busy timeout: deliver what is already claimed, claim no more
    if not blocks:
        return None
    return "\n".join([MEMORY_GUARD_LINE, *blocks])


def _brief_block(chosen: Sequence[tuple[str | None, str, Sequence[Record]]]) -> str | None:
    """The block for a brief: the opening marker, the guard, one group per file (the file, labelled
    with the document that named it when the brief did not, and its top two records), a line naming
    the call that returns the rest, and the closing marker. ``chosen`` is in the order D2 fixes
    (the brief's files, then the hop's) and each file has records.

    Files are taken in order while the record lines stay within six and the whole block within
    3,000 characters. The first file that does not fit ends the list, with one exception: when
    only the line cap stops it and a line is left, its first record fills that line (a file whose
    second record is cut is still the top of its list, and the block shows no per-file counts).
    A file the character cap stops is dropped whole. ``More:`` names exactly the files shown.
    ``None`` when not even the first fits.
    The record lines are the Read path's (``_record_line``); its ``_records_block`` layout is not
    reused, because a brief covers several files.
    see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1, D3)
    """

    def build(groups: list[list[str]], shown: list[str]) -> str:
        more = f"More: get_task_context(files={json.dumps(shown, ensure_ascii=False)})."
        return "\n".join(
            [_BRIEF_OPEN, MEMORY_GUARD_LINE, *(ln for g in groups for ln in g), more, _BRIEF_CLOSE]
        )

    groups: list[list[str]] = []
    shown: list[str] = []
    lines = 0
    for doc, rel, records in chosen:
        # the file's top records, cut to the lines left (the first record fills a last line)
        top = records[: min(_BRIEF_RECORDS_PER_FILE, _BRIEF_MAX_LINES - lines)]
        if not top:
            break
        header = f"{rel}:" if doc is None else f"{rel} (named in {doc}):"
        group = [header, *(_record_line(r) for r in top)]
        if len(build([*groups, group], [*shown, rel])) > _BRIEF_MAX_CHARS:
            break
        groups.append(group)
        shown.append(rel)
        lines += len(top)
    return build(groups, shown) if shown else None


def _agent_brief(payload: dict, hot: _HotOpener) -> dict | None:
    """The ``hookSpecificOutput`` for an ``Agent`` call: its tool input with the records for the
    files its brief names appended to ``prompt``, or ``None`` for nothing to add.

    ``None`` when ``SIDEGRAPH_AGENT_BRIEF`` is ``off``, ``prompt`` is not a string, the prompt
    already holds the opening marker (a brief forwarded from another agent), the store has no
    usable index, or no file the brief names has records. The files are the ones ``text_paths``
    finds in the brief and, one hop away, in the text documents it names (``brief_files``); each
    resolves against the payload's ``cwd`` and then the store's project root, and the suffix
    fallback matches against the one scan of the anchored files. The call's other fields are
    copied through untouched and no ``permissionDecision`` is set. It claims none of the Read
    path's per-agent keys: the parent cannot see the block, so claiming them would only suppress
    the parent's own first delivery.
    see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1-D4)
    """
    if os.environ.get("SIDEGRAPH_AGENT_BRIEF") == "off":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or _BRIEF_OPEN in prompt:
        return None

    from ..config import resolve_store_location
    from ..text_paths import brief_files

    location = resolve_store_location(
        root=os.environ.get("CLAUDE_PROJECT_DIR"), warn_on_create=False, search_ancestors=True
    )
    index = hot(location.path)
    if index is None:
        return None
    anchored = index.anchored_files()
    chosen: list[tuple[str | None, str, list[Record]]] = []
    cwd = payload.get("cwd")
    for doc, rel in brief_files(
        prompt, _touch_root(location), anchored, cwd if isinstance(cwd, str) else None
    ):
        records = index.records_for(anchored[rel]) if rel in anchored else []
        if records:
            chosen.append((doc, rel, records))
            if len(chosen) == _BRIEF_MAX_LINES:  # every file shown costs a line at least
                break
    block = _brief_block(chosen)
    if block is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "prompt": f"{prompt}\n\n{block}"},
        }
    }


def _hook_output(payload: dict, hot: _HotOpener) -> dict:
    """What :func:`pre_tool_use` prints: the Agent branch's ``updatedInput`` for a subagent
    spawn, else the Read path's ``additionalContext``, else ``{}``."""
    if payload.get("tool_name") in _AGENT_TOOLS:
        return _agent_brief(payload, hot) or {}
    text = _delivery(payload, hot)
    if text is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def pre_tool_use() -> None:
    """PreToolUse hook (M6, FR8.3): hand the agent the records anchored to the file it
    is about to read or edit.

    Prints a non-blocking ``additionalContext`` block — no ``permissionDecision`` field is
    emitted, so the call never touches the permission decision (it neither allows, denies, nor
    asks) — for a Read, Grep, Edit, Write, or a Bash line whose ``sed``, ``grep``, ``rg`` or
    ``cat`` names a file with records. Each file is delivered once per agent (the session's own
    agent and each subagent separately; see :func:`_agent_identity`), up to ten files per agent
    and three per call. Switch it off with ``SIDEGRAPH_GREP_NUDGE=off`` (the name is older than
    the behaviour; touch recording is not affected). Must never crash or block the tool call:
    any failure — or nothing to deliver — prints ``{}``; either way the normal permission flow
    applies untouched.

    On an ``Agent`` (or ``Task``) call, the one that spawns a subagent, it prints the call's own
    input with the records for the files the brief names appended to ``prompt`` (``updatedInput``,
    see :func:`_agent_brief`). The subagent receives the block in its first message; the parent's
    view of its own call is unchanged. On by default; ``SIDEGRAPH_AGENT_BRIEF=off`` disables it.

    Runs on every Read, Grep, Edit, Write, Agent and matching Bash call, so it never imports the
    models and never constructs a ``Store``: both the touch row and the delivery go through one
    ``hot_index.HotIndex`` opened once, which trusts the index as the last full open left it and
    does nothing when it cannot use it. A tool call therefore never creates a store (SessionStart
    and the MCP server still do).
    see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D3),
    design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D1-D3) and
    design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1)
    """
    if len(sys.argv) > 1:
        refuse_arguments(
            "sidegraph-pre-tool-use",
            "Hands the agent the records anchored to the file it is about to read or edit.",
        )
    # Payload first: stdin can only be read once, and recording (D4) must see it even when
    # delivery is switched off. Then record, then deliver.
    try:
        payload = _read_payload()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    hot = _HotOpener()
    try:
        _record_touch_event(payload, hot)
        try:
            output = _hook_output(payload, hot)
        except Exception:
            output = {}
        print(json.dumps(output))
    finally:
        hot.close()


def _hook_usage(prog: str, summary: str) -> str:
    return (
        f"usage: {prog} [-h]\n"
        f"{summary} Reads the host's JSON payload on stdin and prints a JSON answer;\n"
        "Claude Code or Codex runs it, not you. Takes no arguments. Documented in\n"
        "docs/reference/hooks.md.\n"
    )


def refuse_arguments(prog: str, summary: str) -> None:
    """Answer an argument on the command line of a hook entry point, then exit.

    The hooks take their input from stdin, so a person's ``--help`` used to run the hook against
    the repository. Called first, before stdin, the store or any cache, and only when ``argv``
    is not empty, so the no-argument hot path costs one list check. ``-h``/``--help`` prints
    the usage and exits 0 when it appears anywhere in the arguments (as ``sidegraph-mcp`` and
    ``sidegraph-prepare-commit-msg`` treat it); anything else is a usage error naming the first
    argument on stderr and exits 2.
    """
    args = sys.argv[1:]
    usage = _hook_usage(prog, summary)
    if any(a in ("-h", "--help") for a in args):
        sys.stdout.write(usage)
        raise SystemExit(0)
    sys.stderr.write(f"{prog}: error: unexpected argument '{args[0]}'\n{usage}")
    raise SystemExit(2)


def _read_payload() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}
