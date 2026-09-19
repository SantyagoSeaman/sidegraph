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
  ``additionalContext`` nudge (never denies/blocks the tool call), at most once per session,
  when the store has decision memory to offer.

All three are wired as of M6.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import NamedTuple

# Defined in config.py, not here: server.py (core) needs this constant too (Task 4), and
# core importing from host/ would invert this project's seam rule (host may depend on the
# core, never the reverse). Re-exported so `hooks.TELEMETRY_SESSION_KEY` still resolves for
# callers and tests that reach it through this module.
from ..config import TELEMETRY_SESSION_KEY

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
# nudge in `pre_tool_use` below only ever covers those two tools and only fires once; this
# is the behavioral instruction that's meant to cover the rest by telling the agent, not by
# gating a specific tool call). Prepended at the hook-assembly level (here, in
# `session_start`) rather than inside `retrieval.render_toc`/`top_tier_map` themselves, so
# both renderers stay pure content-formatters and this line is written/tested in exactly one
# place regardless of which of the two produced the rest of the text.
STANDING_SEARCH_INSTRUCTION = (
    "When you need to find or understand code in this project, call get_task_context(seeds) "
    "before any grep or file search — decisions, gotchas and a domain map are indexed here."
)

# D7.2 (staleness-machinery wave, E7 observation): a user's own project/personal
# settings.json can register a SECOND SessionStart hook alongside the plugin's, firing
# session_start() twice for one logical session — a double map injection. ONE bounded
# meta key (never a PER-SESSION key: meta rows are never pruned, so a per-session key
# grows forever — fact `01KYFYMB0…` in this store is the reason the *value*, never
# key-absence, carries the guard) holding ``<session_id>|<iso-timestamp>``. Precedent for
# hooks writing meta: the pre_tool_use one-shot ledger (`_PRETOOL_NUDGE_KEY_PREFIX` below)
# and `TELEMETRY_SESSION_KEY` (config.py).
_SESSION_START_KEY = "session_start"
_SESSION_START_DEDUPE_SECONDS = 60


def _session_start_duplicate(store, session_id: str, now: datetime) -> bool:
    """True iff ``session_start()`` already ran for ``session_id`` within the last
    :data:`_SESSION_START_DEDUPE_SECONDS` (design D7.2). Same session id AND a fresh
    timestamp -> duplicate, ledger left untouched (so a third rapid call still compares
    against the ORIGINAL write, not a duplicate's own arrival time); anything else
    (no prior key, a different session, or an expired window) -> not a duplicate, and the
    fresh marker is written here as the "else write" half of "same session id AND
    timestamp < 60s old -> exit silently; else write and emit". Best-effort: any
    read/parse/write failure is treated as "not a duplicate" (never blocks the session).

    A NAIVE-but-parseable ``prev_stamp`` (schema requires aware timestamps, but the
    ledger is a bare string a hand edit or an older format could still leave naive) is
    explicitly treated as "not a duplicate" via the ``tzinfo is not None`` guard below —
    the same guard ``capture._session_id_fallback`` already applies for its own stamp
    comparison — rather than ever attempting ``now - prev_dt`` (CORRECTION-6, code
    review): that subtraction raises ``TypeError`` on a naive/aware mismatch, which is
    NOT a ``ValueError`` the inner ``except`` catches, so it used to escape straight to
    the outer ``except Exception`` and skip the ``set_meta`` write below entirely — the
    ledger got stuck on the bad stamp forever, never self-healing. Falling through to the
    write instead means the very next call is compared against a freshly-written, valid
    stamp."""
    try:
        raw = store.get_meta(_SESSION_START_KEY)
        if raw is not None:
            prev_id, _, prev_stamp = raw.partition("|")
            if prev_id == session_id:
                try:
                    prev_dt = datetime.fromisoformat(prev_stamp)
                    if (
                        prev_dt.tzinfo is not None
                        and (now - prev_dt).total_seconds() < _SESSION_START_DEDUPE_SECONDS
                    ):
                        return True
                except ValueError:
                    pass
        store.set_meta(_SESSION_START_KEY, f"{session_id}|{now.isoformat()}")
        return False
    except Exception:
        return False


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


def _transcript_stats(transcript_path: str) -> _TranscriptStats:
    """One pass over a transcript JSONL file: real user prompts (``_is_real_user_prompt``),
    assistant ``tool_use`` blocks, and the first-seen top-level ``entrypoint`` field.

    A single malformed line is skipped rather than fatal, so one corrupt row doesn't zero
    out an otherwise-substantial session (the skip is shared by all three counts — one
    parse, one policy). A missing/unreadable file (bad path, permissions) propagates to
    the caller -- ``stop``'s outer ``try/except`` turns that into the same "print {} and
    allow" as any other never-crash failure. A transcript with no ``entrypoint`` anywhere
    returns ``None`` — the caller degrades to the prompt-only gate, never to arming.
    """
    real_prompts = 0
    tool_uses = 0
    entrypoint: str | None = None
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
            if _is_real_user_prompt(entry):
                real_prompts += 1
            elif entry.get("type") == "assistant":
                message = entry.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), list):
                    tool_uses += sum(
                        1
                        for block in message["content"]
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    )
    return _TranscriptStats(real_prompts, tool_uses, entrypoint)


def session_start() -> None:
    """SessionStart hook (Stage 4): inject the phase-1 top-tier map as additionalContext.

    Reads $SIDEGRAPH_DIR (or legacy $SIDEGRAPH_DB) / $SIDEGRAPH_GRAPH (same env, same
    resolution, as the server and CLI — see ``config.resolve_store_path``). Must never
    crash the session — any failure prints ``{}`` and exits 0.

    Double-injection dedupe (design D7.2): a session id repeated within
    :data:`_SESSION_START_DEDUPE_SECONDS` exits silently, since a user's own hook
    registration can fire this alongside the plugin's for one logical session — see
    :func:`_session_start_duplicate`.
    """
    try:
        from ..config import resolve_store_path
        from ..engine.reader import GraphifyReader
        from ..retrieval import TOC_CACHE_KEY, build_toc, render_toc, top_tier_map
        from ..store import Store

        payload = _read_payload()
        store = Store(resolve_store_path(root=os.environ.get("CLAUDE_PROJECT_DIR")))
        session_id = payload.get("session_id")

        # D7.2: double-injection dedupe, checked BEFORE any other work (sync/TOC/nudges) —
        # a duplicate SessionStart call for the same session within the window exits
        # silently, same "print {} and return" shape the Stop hook's own one-shot ledger
        # uses. Gated on a real session id (see _session_start_duplicate's docstring).
        if (
            isinstance(session_id, str)
            and session_id
            and _session_start_duplicate(store, session_id, datetime.now(UTC))
        ):
            print(json.dumps({}))
            return

        # Own try/except, like the ratification nudge below: telemetry must never cost the
        # map, which is this hook's only real deliverable.
        try:
            if isinstance(session_id, str) and session_id:
                # D7.3 (staleness-machinery wave, E8: author=None session=None on
                # Stop-channel captures): this write is now UNCONDITIONAL — no longer
                # gated on telemetry_enabled() — because capture._propose_one falls back
                # to it for provenance.session_id when the caller passed none (best-effort
                # attribution, not security; see that fallback's own docstring).
                store.set_meta(
                    TELEMETRY_SESSION_KEY,
                    f"{session_id}|{datetime.now(UTC).isoformat()}",
                )
            # Pruning is deliberately NOT gated on telemetry_enabled() (practitioner
            # re-review round 2): it used to be, which meant opting out froze the 30-day
            # retention of whatever journal already existed — an opt-out that makes the
            # data live LONGER is the wrong way round for GDPR and for a works council.
            # Recording stays off when the flag is off; expiry keeps running regardless,
            # so opting out strictly reduces what is retained.
            store.prune_telemetry_events()
        except Exception:
            pass

        try:
            reader = GraphifyReader(_env_path("SIDEGRAPH_GRAPH", "graphify-out/graph.json"))
        except Exception:
            reader = None
        try:
            from .. import sync as _sync

            _sync.maybe_sync(store, reader)
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
        if isinstance(cache, dict) and cache.get("domains"):
            try:
                # Domain/initiative summaries remain cache-owned, but unratified global
                # mistakes must reflect the live Store even when the graph is unavailable
                # and lazy sync cannot refresh an older cache that predates this key.
                cache = {
                    **cache,
                    "unratified": build_toc(store).get("unratified", []),
                }
                text = render_toc(cache)
            except Exception:
                text = None
        if text is None:
            text = top_tier_map(store, reader)
        # Standing instruction goes first, ahead of either renderer's content — see
        # STANDING_SEARCH_INSTRUCTION's docstring for why it lives here and not in the
        # renderers.
        text = f"{STANDING_SEARCH_INSTRUCTION}\n\n{text}"

        # Pending-ratification queue visibility (A): one line, own try/except -- a count
        # failure must never cost the map. SIDEGRAPH_RATIFY_NUDGE=off suppresses it (same
        # convention as SIDEGRAPH_CAPTURE_NUDGE/SIDEGRAPH_GREP_NUDGE; read at point of use,
        # never cached). See design/superpowers/specs/
        # 2026-07-10-ratification-ux-and-mcp-gaps-design.md.
        if os.environ.get("SIDEGRAPH_RATIFY_NUDGE") != "off":
            try:
                nd, nf, ndom = store.pending_ratification_counts()
                total = nd + nf + ndom
                if total:
                    # Oldest-age suffix (2026-08-04 proposal-lifecycle design D3): queue
                    # AGE, not just size, is what makes neglect visible — proposals older
                    # than the surfacing window still count here even though they no
                    # longer render as content (the counter is metadata; regulated mode
                    # and the window never silence it). Domains carry no valid_from and
                    # are excluded from the age scan, never guessed.
                    oldest = ""
                    stamps = [d.valid_from for d in store.iter_proposed()]
                    stamps += [f.valid_from for f in store.iter_proposed_facts()]
                    if stamps:
                        age_days = max(0, (datetime.now(UTC) - min(stamps)).days)
                        oldest = f"; oldest {age_days} days"
                    text += (
                        f"\n\nSidegraph: {total} record(s) awaiting ratification "
                        f"({nd} decisions, {nf} facts, {ndom} domains{oldest}) — review "
                        "with the ratify MCP tool or sidegraph-ratify."
                    )
            except Exception:
                pass  # the pending line must never cost the map

        # Drift line (drift→supersede D4): the refresh runs UNCONDITIONALLY — it is what
        # keeps the retrieval markers' cache fresh; SIDEGRAPH_DRIFT_NUDGE=off gates the
        # PROSE only (spec I5, option b — gating the refresh froze markers at their last
        # pre-off state with no self-heal path). Own try/except: a drift failure must
        # never cost the map.
        try:
            from .. import sync as _sync_drift

            n = _sync_drift.refresh_code_drift_cache(store)
            if n and os.environ.get("SIDEGRAPH_DRIFT_NUDGE") != "off":
                text += (
                    f"\n\nSidegraph: {n} record(s) are anchored to code that changed "
                    "after their capture — task-relevant ones carry a [drifted] tag in "
                    "retrieval; full list: sidegraph-doctor; supersede any that no "
                    "longer hold."
                )
        except Exception:
            pass  # the drift line must never cost the map

        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": text,
                    }
                }
            )
        )
    except Exception:
        print(json.dumps({}))


def stop() -> None:
    """Stop hook (Stage 5): guarded block-to-distill.

    Fires the distillation nudge at most once per session (capture ledger +
    ``stop_hook_active``), and only once the session looks substantial: >=
    ``_MIN_USER_PROMPTS`` real prompts, OR — one-shot (``sdk-cli``) sessions only —
    >= ``_MIN_TOOL_USES`` assistant tool calls (see ``_transcript_stats`` and the
    scoping rationale above ``_MIN_TOOL_USES``). Disable entirely with
    ``SIDEGRAPH_CAPTURE_NUDGE=off``. Must never crash the session — any failure allows the
    stop.

    Ordering matters: the substance gate runs BEFORE ``mark_captured`` is ever written, and
    a gate failure returns without touching the ledger at all. A session gated at turn 1 (not
    yet substantial) is therefore still eligible to nudge later once it becomes substantial —
    marking it early would have permanently suppressed that later nudge.
    """
    try:
        if os.environ.get("SIDEGRAPH_CAPTURE_NUDGE") == "off":
            print(json.dumps({}))
            return

        from ..config import resolve_store_path
        from ..store import Store

        payload = _read_payload()
        if payload.get("stop_hook_active"):
            print(json.dumps({}))
            return
        session_id = payload.get("session_id")
        if not session_id:
            print(json.dumps({}))
            return

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
        substantial = stats.real_prompts >= _MIN_USER_PROMPTS or (
            stats.entrypoint == _ONE_SHOT_ENTRYPOINT and stats.tool_uses >= _MIN_TOOL_USES
        )
        if not substantial:
            print(json.dumps({}))  # not substantial yet -- do NOT mark_captured
            return

        store = Store(resolve_store_path(root=os.environ.get("CLAUDE_PROJECT_DIR")))
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

# Meta-table key prefix (session-scoped, like the Stop hook's capture ledger) — deliberately
# NOT the Stop hook's `capture_sessions` table: that table means "already nudged to
# *distill*", a different guard. Reusing it here would make the first blind Read of a
# session silently consume the Stop-hook's one-shot distillation nudge too. `store.get_meta`/
# `set_meta` already exist for exactly this kind of session/process-scoped marker, so no new
# table (and no SCHEMA_VERSION bump) is needed.
_PRETOOL_NUDGE_KEY_PREFIX = "pretool_nudge:"

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


def _record_touch_event(payload: dict) -> None:
    """Record one touch. Runs FIRST in ``pre_tool_use``, before every nudge gate (D4).

    The nudge's early exits belong to the nudge: inheriting them would record nothing for
    Edit/Write, nothing after a session's first Read, nothing in a store without memory yet,
    and nothing when the grep nudge is disabled. The single gate shared with the nudge is
    ``session_id``, because a touch that cannot be attributed cannot be written at all.
    Never raises (D9) — this runs inside a PreToolUse hook, where an exception would
    interfere with the user's own tool call.
    """
    try:
        from ..config import resolve_store_path, telemetry_enabled

        if not telemetry_enabled():
            return
        tool = payload.get("tool_name")
        if tool not in _TOUCH_TOOLS:
            return
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        path = _touch_path(payload.get("tool_input"), root)
        if path is None:
            return

        from ..store import Store

        # warn_on_create=False: recording runs on every matching tool call regardless of the
        # nudge's own gates (D4), including SIDEGRAPH_GREP_NUDGE=off. Before this feature, a
        # Read/Grep on a store-less repo with the nudge silenced returned at the env check
        # before ever constructing a Store, so no warning fired. Recording now constructs one
        # unconditionally on that same path, and printing "creating new store" stderr noise
        # there would land on a path the user explicitly silenced and cannot act on.
        store = Store(
            resolve_store_path(root=os.environ.get("CLAUDE_PROJECT_DIR"), warn_on_create=False)
        )
        store.record_touch(session_id, path, str(tool))
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
# bounded keys, one nudge of each kind per session at most.
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
    asks) — at most once per session, when ALL hold: the tool is Read or Grep, the call targets
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
        session_id = payload.get("session_id")
        if not session_id:
            print(json.dumps({}))
            return

        from ..config import resolve_store_path
        from ..schema import DecisionStatus, DomainStatus
        from ..store import Store

        store = Store(resolve_store_path(root=os.environ.get("CLAUDE_PROJECT_DIR")))
        ledger_key = f"{_PRETOOL_NUDGE_KEY_PREFIX}{session_id}"
        specific_key = f"{_PRETOOL_SPECIFIC_KEY_PREFIX}{session_id}"
        rel = _touch_path(payload.get("tool_input"), _env_path("CLAUDE_PROJECT_DIR", os.getcwd()))
        titles = _titles_for_path(store, rel) if rel else []
        # Each form has its own one-shot key: a generic nudge early in a session must not
        # consume the path-specific one the session may earn later (review finding 3).
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
        # ledger keys are in the store; the transcript never shows it, since Claude Code
        # does not echo PreToolUse additionalContext) and the agent read the file anyway.
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

        # before emitting: a crash cannot double-nudge
        store.set_meta(specific_key if titles else ledger_key, "1")
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


def _env_path(var: str, default: str) -> str:
    """Resolve a host-configured path. Relative values anchor to $CLAUDE_PROJECT_DIR when
    the host provides it (plugin hooks may run with an arbitrary cwd), else to cwd as before.
    """
    value = os.environ.get(var, default)
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if root and not os.path.isabs(value):
        return os.path.join(root, value)
    return value
