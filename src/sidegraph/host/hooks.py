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
- ``pre_tool_use`` (M6, FR8.3): redirect blind Read/Grep toward retrieval — a non-blocking
  ``additionalContext`` nudge (never denies/blocks the tool call), at most once per agent
  (the session's own agent and each subagent separately), when the store has decision memory
  to offer.

All three are wired as of M6.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
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
    ancestor_store,
)

if TYPE_CHECKING:
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
# grep/rg/find, MCP structure-query tools, everything -- not just Read/Grep (the PreToolUse
# nudge in `pre_tool_use` below only ever covers those two tools and fires once per agent; this
# is the behavioral instruction that's meant to cover the rest by telling the agent, not by
# gating a specific tool call). Prepended at the hook-assembly level (here, in
# `session_start`) rather than inside `retrieval.render_toc`/`top_tier_map` themselves, so
# both renderers stay pure content-formatters and this line is written/tested in exactly one
# place regardless of which of the two produced the rest of the text.
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

# D7.2 (staleness-machinery wave, E7 observation): a user's own project/personal
# settings.json can register a SECOND SessionStart hook alongside the plugin's, firing
# session_start() twice for one logical session — a double map injection. ONE bounded
# meta key (never a PER-SESSION key: meta rows are not pruned in general, so a per-session
# key grows forever — fact `01KYFYMB0…` in this store is the reason the *value*, never
# key-absence, carries the guard; only the PreToolUse nudge prefixes are expired, by
# `prune_meta_prefixes` at SessionStart) holding ``<session_id>|<iso-timestamp>``. Precedent for
# hooks writing meta: the pre_tool_use one-shot ledger (`_PRETOOL_NUDGE_KEY_PREFIX` below)
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


def _nudge_key(prefix: str, session_id: str, agent_id: str | None) -> str:
    """A PreToolUse one-shot key: ``prefix + session`` for the session's own agent, which is
    the key every older version wrote, and ``prefix + session:agent`` for a subagent."""
    return f"{prefix}{session_id}" if agent_id is None else f"{prefix}{session_id}:{agent_id}"


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


def host_checks(location: StoreLocation) -> tuple[Check, ...]:
    """The integrity checks that need the host: ``stray-store``, a closure over this launch's
    ``StoreLocation`` because the portable registry's ``Inputs`` stays host-free. The hook
    passes ``CHECKS[:5] + host_checks(location) + CHECKS[5:]`` to keep the line order.
    see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D1, D4 check 5)
    """
    from ..integrity import Check, Inputs, Problem

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

    return (Check("stray-store", frozenset({"session"}), detect_stray_store),)


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


# Host-emitted slash-command wrappers persist as `type: "user"` entries with NO
# distinguishing flag (measured on real session files, 2026-07-30) -- content prefix is
# the only way to tell them from a typed prompt. A genuine prompt starting with one of
# these literals would merely raise the nudge threshold by one; this is a heuristic gate,
# not provenance.
_HOST_EMITTED_PREFIXES = ("<command-name>", "<local-command-stdout>", "<local-command-caveat>")


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
    """
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
        # The PreToolUse one-shot keys are one per agent now (about a hundred a day in a
        # session that spawns subagents), so they expire like the journals do. Its own try:
        # the journal prune above failing must not leave these to pile up, and vice versa.
        with contextlib.suppress(Exception):
            store.prune_meta_prefixes(
                (_PRETOOL_NUDGE_KEY_PREFIX, _PRETOOL_SPECIFIC_KEY_PREFIX),
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
        # renderers.
        text = f"{STANDING_SEARCH_INSTRUCTION}\n\n{text}"

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


def stop() -> None:
    """Stop hook (Stage 5): guarded block-to-distill.

    Fires the distillation nudge at most once per session (capture ledger +
    ``stop_hook_active``), and only once the session looks substantial: >=
    ``_MIN_USER_PROMPTS`` real prompts, OR — one-shot (``sdk-cli``) sessions only —
    >= ``_MIN_TOOL_USES`` assistant tool calls (see ``_transcript_stats``, ``_is_substantial``
    and the scoping rationale above ``_MIN_TOOL_USES``). It reads a Claude Code transcript and
    a Codex rollout alike; on Codex only a person's interactive thread can arm. Disable
    entirely with ``SIDEGRAPH_CAPTURE_NUDGE=off``. Must never crash the session — any failure
    allows the stop.

    Ordering matters: the substance gate runs BEFORE ``mark_captured`` is ever written, and
    a gate failure returns without touching the ledger at all. A session gated at turn 1 (not
    yet substantial) is therefore still eligible to nudge later once it becomes substantial —
    marking it early would have permanently suppressed that later nudge.
    """
    try:
        if os.environ.get("SIDEGRAPH_CAPTURE_NUDGE") == "off":
            print(json.dumps({}))
            return

        from ..config import resolve_store_location
        from ..gitio import open_index_ro
        from ..store import Store

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
        # to the authoritative `was_captured` check below; the peek never writes.
        peek = open_index_ro(
            Path(
                resolve_store_location(
                    root=os.environ.get("CLAUDE_PROJECT_DIR"),
                    warn_on_create=False,
                    search_ancestors=True,
                ).path
            )
        )
        if peek is not None:
            try:
                if peek.execute(
                    "SELECT 1 FROM capture_sessions WHERE session_id = ?", (session_id,)
                ).fetchone():
                    print(json.dumps({}))
                    return
            except sqlite3.Error:
                pass
            finally:
                peek.close()

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

        store = Store(
            resolve_store_location(
                root=os.environ.get("CLAUDE_PROJECT_DIR"), search_ancestors=True
            ).path
        )
        if store.was_captured(session_id):
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
                # 601 + 183 = 784 <= 800 at n=5 — inside the staleness-wave's pinned
                # anti-creep bound (test_host_stop.py's <=800 assert covers the
                # concatenation).
                reason += (
                    f" Also: {n} drifted record(s) — their anchored code changed after "
                    "capture; if this session's work overtook any of them, "
                    "supersede_decision it (ids: get_task_context or sidegraph-doctor)."
                )
        except Exception:
            pass

        store.mark_captured(session_id)  # before emitting: a crash cannot double-nudge
        print(json.dumps({"decision": "block", "reason": reason, "suppressOutput": True}))
    except Exception:
        print(json.dumps({}))


# Exact-match set (§5 FR8.3): only Read/Grep are blind-reading tools worth redirecting —
# Bash/Edit/Write etc. are left alone (a matcher-level filter also does this in hooks.json;
# this is the belt-and-braces check inside the hook itself).
_PRETOOL_NUDGE_TOOLS = frozenset({"Read", "Grep"})

# Meta-table key prefix (agent-scoped: `_nudge_key` names the session and, for a subagent, the
# agent; the Stop hook's capture ledger is session-scoped) — deliberately
# NOT the Stop hook's `capture_sessions` table: that table means "already nudged to
# *distill*", a different guard. Reusing it here would make the first blind Read of a
# session silently consume the Stop-hook's one-shot distillation nudge too. `store.get_meta`/
# `set_meta` already exist for exactly this kind of session/process-scoped marker, so no new
# table (and no SCHEMA_VERSION bump) is needed.
_PRETOOL_NUDGE_KEY_PREFIX = "pretool_nudge:"

# SessionStart expires a one-shot key this many days after its claim. The value is the claim's
# ISO timestamp (the keys used to hold "1", which SessionStart also expires).
_PRETOOL_KEY_RETENTION_DAYS = 30

# Recording is a different mechanism from the nudge and needs a different tool set: an
# Edit/Write is the strongest available evidence that the agent worked somewhere, while the
# nudge is only about blind *reading*. Kept as its own frozenset because `hooks.json`'s
# matcher is user-editable — trusting the matcher alone would record arbitrary tools in a
# hand-wired setup, the same belt-and-braces reasoning `_PRETOOL_NUDGE_TOOLS` already applies.
_TOUCH_TOOLS = frozenset({"Read", "Grep", "Edit", "Write"})


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


def _record_touch_event(payload: dict) -> None:
    """Record one touch. Runs FIRST in ``pre_tool_use``, before every nudge gate (D4).

    The nudge's early exits belong to the nudge: inheriting them would record nothing for
    Edit/Write, nothing after an agent's first Read, nothing in a store without memory yet,
    and nothing when the grep nudge is disabled. The single gate shared with the nudge is
    ``session_id``, because a touch that cannot be attributed cannot be written at all.
    Never raises (D9) — this runs inside a PreToolUse hook, where an exception would
    interfere with the user's own tool call.
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
        # warn_on_create=False: see the note on the Store below. Resolved before the path is
        # made because the touch root depends on where the store was found (spec D2).
        location = resolve_store_location(
            root=os.environ.get("CLAUDE_PROJECT_DIR"), warn_on_create=False, search_ancestors=True
        )
        path = _touch_path(payload.get("tool_input"), _touch_root(location))
        if path is None:
            return

        from ..store import Store

        # warn_on_create=False: recording runs on every matching tool call regardless of the
        # nudge's own gates (D4), including SIDEGRAPH_GREP_NUDGE=off. Before this feature, a
        # Read/Grep on a store-less repo with the nudge silenced returned at the env check
        # before ever constructing a Store, so no warning fired. Recording now constructs one
        # unconditionally on that same path, and printing "creating new store" stderr noise
        # there would land on a path the user explicitly silenced and cannot act on.
        store = Store(location.path)
        store.record_touch(session_id, path, str(tool), agent=agent_id)
    except Exception:
        return


def _looks_like_source_target(tool_input: object) -> bool:
    """Permissive on purpose (§5 FR8.3: "be permissive, any string arg"): this only screens
    out a malformed or empty payload — not any particular path shape. The named keys are
    Read/Grep's usual ones, but the catch-all below passes ANY dict with a non-empty string
    value (a ``{"command": ...}`` payload sails through, review 2026-08-08) — the tool-set
    check in ``pre_tool_use`` is the gate that decides which tools nudge, never this."""
    if not isinstance(tool_input, dict):
        return False
    for key in ("file_path", "path", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return any(isinstance(v, str) and v.strip() for v in tool_input.values())


# How many record titles the path-specific nudge names. Two: enough to show the store has
# something concrete, short enough that the whole nudge stays readable at a glance — the
# thing the counting form was not.
_PRETOOL_TITLE_CAP = 2

# Per-title clip, so one ADR-scale title cannot swallow the nudge. Matches the tight
# render tier retrieval uses for the same job (`retrieval._LINE_CLIP_CHARS` is 240 for a
# whole line; a title inside a two-title nudge gets less).
_PRETOOL_TITLE_CHARS = 90

# The path-specific nudge gets its OWN one-shot key (review finding 3). Sharing the
# generic ledger meant a session whose first Read landed on an unanchored file — measured:
# 94% of first reads on this repo, because sessions open a spec or a plan first — burned
# its single nudge on the counting form and could never receive the specific one. Two
# keys per agent, one nudge of each kind per agent at most.
_PRETOOL_SPECIFIC_KEY_PREFIX = "pretool_nudge_path:"


def _titles_for_path(store, rel_path: str) -> list[str]:
    """Titles of live decisions anchored to ``rel_path``, accepted memory first and
    proposed memory last, clipped and capped.

    Mistakes-first is the product's one hard ranking guarantee (``retrieval._MISTAKE_KINDS``
    — imported, never re-spelled, so the two definitions cannot drift). WITHIN each bucket
    the order is newest-first: the scan order is ULID mint order, so without this the cap
    spent itself on the two OLDEST records of a busy path (review finding 2 measured the
    two oldest of eighteen on ``store.py``).

    Pure read. A row that fails to parse is skipped individually — wrapping the whole walk
    in one guard let a single bad row zero the titles for every path (review finding 8).
    """
    from ..retrieval import _MISTAKE_KINDS, partition_by_trust

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
                (mistakes if d.kind in _MISTAKE_KINDS else rest).append(d)
        except Exception:
            continue
    accepted_mistakes, proposed_mistakes = partition_by_trust(mistakes)
    accepted_rest, proposed_rest = partition_by_trust(rest)
    ordered = sorted(accepted_mistakes, key=lambda d: d.valid_from, reverse=True)
    ordered += sorted(accepted_rest, key=lambda d: d.valid_from, reverse=True)
    ordered += sorted(
        [*proposed_mistakes, *proposed_rest], key=lambda d: d.valid_from, reverse=True
    )
    proposed_ids = {d.id for d in [*proposed_mistakes, *proposed_rest]}

    out: list[str] = []
    for d in ordered[:_PRETOOL_TITLE_CAP]:
        # Newlines and quotes in a title would break the emitted line and its quoting
        # (review finding 7); the `[unratified]` tag is the same signal retrieval always
        # attaches to a PROPOSED record (finding 5) — the nudge must not present an
        # unreviewed draft as settled memory.
        title = " ".join(d.title.split()).replace('"', "'")
        if len(title) > _PRETOOL_TITLE_CHARS:
            title = title[: _PRETOOL_TITLE_CHARS - 1].rstrip() + "…"
        if d.id in proposed_ids:
            title += " [unratified]"
        out.append(title)
    return out


def pre_tool_use() -> None:
    """PreToolUse hook (M6, FR8.3): redirect blind Read/Grep toward retrieval.

    Fires a non-blocking ``additionalContext``-only nudge — no ``permissionDecision`` field is
    emitted, so the call never touches the permission decision (it neither allows, denies, nor
    asks) — at most once per agent (the session's own agent and each subagent separately; see
    :func:`_agent_identity`), when ALL hold: the tool is Read or Grep, the call targets
    something that looks like a file/pattern string, and the store has >= 1 accepted domain OR
    >= 1 valid decision (nothing to redirect to otherwise). Disable entirely with
    ``SIDEGRAPH_GREP_NUDGE=off``. Must never crash or block the tool call: any failure — or any
    of the above conditions not holding — prints ``{}``; either way the normal permission flow
    applies untouched.
    """
    # Payload first: stdin can only be read once, and recording (D4) must see it even when
    # the nudge is disabled. Then record, then run the nudge branch exactly as before.
    try:
        payload = _read_payload()
    except Exception:
        payload = {}
    _record_touch_event(payload)

    try:
        if os.environ.get("SIDEGRAPH_GREP_NUDGE") == "off":
            print(json.dumps({}))
            return
        if payload.get("tool_name") not in _PRETOOL_NUDGE_TOOLS:
            print(json.dumps({}))
            return
        if not _looks_like_source_target(payload.get("tool_input")):
            print(json.dumps({}))
            return
        session_id, _ = _session_identity(payload)
        if not session_id:
            print(json.dumps({}))
            return

        from ..config import resolve_store_location
        from ..schema import DecisionStatus, DomainStatus
        from ..store import Store

        location = resolve_store_location(
            root=os.environ.get("CLAUDE_PROJECT_DIR"), search_ancestors=True
        )
        store = Store(location.path)
        agent_id = _agent_identity(payload)
        ledger_key = _nudge_key(_PRETOOL_NUDGE_KEY_PREFIX, session_id, agent_id)
        specific_key = _nudge_key(_PRETOOL_SPECIFIC_KEY_PREFIX, session_id, agent_id)
        rel = _touch_path(payload.get("tool_input"), _touch_root(location))
        titles = _titles_for_path(store, rel) if rel else []
        # Each form has its own one-shot key: a generic nudge early in a session must not
        # consume the path-specific one the session may earn later (review finding 3). This
        # read only spares the work below once the key is spent; the claim at the end decides.
        if store.get_meta(specific_key if titles else ledger_key):
            print(json.dumps({}))
            return

        domains = list(store.iter_domains(status=DomainStatus.ACCEPTED))
        now = datetime.now(UTC)
        decisions = [
            d
            for d in store.iter_decisions()
            if d.status not in (DecisionStatus.SUPERSEDED, DecisionStatus.REJECTED)
            and (d.valid_to is None or d.valid_to > now)
        ]
        if not domains and not decisions:
            print(json.dumps({}))
            return

        # Path-specific first (whitepaper Phase-1 finding): name what memory HOLDS about
        # the file being opened. The counting form measurably did not work — it fired (its
        # ledger keys are in the store) and the agent read the file anyway. Claude Code
        # 2.1.259+ records the injected context in the transcript (`hook_success` and
        # `hook_additional_context` attachments); before that the transcript never showed it.
        if titles:
            quoted = "; ".join(f'"{t}"' for t in titles)
            text = (
                f"Sidegraph: memory has {quoted} anchored to {rel} — "
                "call get_task_context before reading it blind."
            )
        else:
            # Nothing recorded about THIS path (or a pattern-only Grep with no path):
            # keep the pre-fix generic form. Staying SILENT here was tried and rejected as
            # out of scope — it changes documented behaviour rather than the wording, and
            # the measured cost of the generic nudge is ~30 tokens, not the +5% regime
            # (that is the SessionStart map). Left as a separate, measurable follow-up.
            text = (
                "Sidegraph: this project has a decision memory — call "
                "get_task_context/drill_down before blind reading; "
                f"{len(domains)} domains, {len(decisions)} decisions."
            )

        # Claim BEFORE emitting, in one statement: a crash cannot double-nudge, and two
        # parallel reads of one agent (a subagent opens with two to four) cannot both pass
        # the read above and both nudge. The value is the claim time, which SessionStart
        # uses to expire the key.
        if not store.claim_meta(specific_key if titles else ledger_key, now.isoformat()):
            print(json.dumps({}))
            return
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": text,
                    }
                }
            )
        )
    except Exception:
        print(json.dumps({}))


def _read_payload() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}
