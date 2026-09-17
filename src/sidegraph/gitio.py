"""Git bindings seam (design/superpowers/specs/2026-08-07-git-bindings-design.md) --
commit trailers (П1, ``sidegraph-prepare-commit-msg``) + line-level blame (П2,
``sidegraph-blame``).

The ONLY module that runs git subprocesses FOR THESE TWO FEATURES (``capture.
_capture_commit`` and ``verify._find_repo_root`` already shell out for their own needs --
review M9; this module never reaches into either). Portable core (``schema``), ``engine/``,
and ``host/`` stay untouched -- CLI entry points live in ``cli.py``.

**Read-only over RECORDS** (§0): neither feature ever creates, mutates, or supersedes a
store record. Opening a :class:`~sidegraph.store.Store` is NOT filesystem-read-only --
its ``__init__`` can run an index rebuild (``_refresh_freshness`` ->
``_reload_index_from_canonical``, a real write) -- so this module never constructs one.
Instead :func:`open_index_ro` opens ``index.db`` directly, read-only, via a
``file:...?mode=ro&immutable=0`` URI with a short busy-timeout fallback (``timeout=0.5``,
vs. Python's ``sqlite3`` default of 5.0s against a concurrent writer's lock -- a 5-second
``git commit`` stall is the uninstall failure mode as surely as an error). No index
rebuild ever happens here: if the index is absent or the ro-open fails (including timing
out), every caller degrades to writing/resolving nothing rather than raising.

The prepare-commit-msg hook additionally bounds itself to a hard wall-clock budget (2s,
:data:`HOOK_WALL_CLOCK_BUDGET_SECONDS`) -- see :func:`collect_prepare_commit_candidates`
and :func:`apply_prepare_commit_msg`, both of which re-check the deadline between stages
and give up (write nothing) rather than run over. ``sidegraph-blame`` (П2) is an explicit,
interactive user command, not a commit-path hook, so it carries no such budget -- but it
still never opens a writable ``Store()``, for the same read-only-over-records reason.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Per-subprocess ceiling (several git calls run per hook/blame invocation) -- never the
# unbounded default.
_GIT_TIMEOUT_SECONDS = 1.0

# §0: sqlite ro-open busy-timeout fallback -- default Python sqlite3 timeout is 5.0s.
RO_OPEN_TIMEOUT_SECONDS = 0.5

# §0/§1 step 3: the hook's hard wall-clock budget -- after this, write nothing, exit 0.
HOOK_WALL_CLOCK_BUDGET_SECONDS = 2.0

# §1 step 1(b): "cap at the 5 strongest by weight."
STAGED_CANDIDATE_CAP = 5

# §1 step 2: candidate title/statement truncation.
LABEL_MAX_CHARS = 60

# §1 step 2: the fixed trailer key П2's blame join literal resolves against.
TRAILER_KEY = "Sidegraph-Decision"

_HEADER_LINE = "Sidegraph: uncomment the trailers this commit actually implements"

# П2 output cap (review M8): 50 hunk rows / 6000 chars, whichever comes first.
BLAME_ROW_CAP = 50
BLAME_CHAR_CAP = 6000


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run one git subprocess, never raising. ``None`` on any failure (git missing, cwd
    unusable, timeout) -- every caller treats that the same as a clean "nothing to
    report" degrade (G9)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def head_commit(cwd: Path) -> str | None:
    """``git rev-parse HEAD`` -- ``None`` on an empty repo (no commits yet) or any
    failure (review M7: rule (a) and its fallback are skipped entirely when this is
    ``None``)."""
    result = _run_git(["rev-parse", "HEAD"], cwd)
    if result is None or result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def head_commit_time(cwd: Path) -> datetime | None:
    """HEAD's committer timestamp, for rule (a)'s pre-П0 fallback (records with
    ``provenance.commit is None`` created after this count as captured-since-last-commit
    too)."""
    result = _run_git(["log", "-1", "--format=%cI", "HEAD"], cwd)
    if result is None or result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def staged_files(cwd: Path) -> list[str]:
    """``git diff --cached --name-only`` -- repo-relative paths, deduped order-preserving."""
    result = _run_git(["diff", "--cached", "--name-only"], cwd)
    if result is None or result.returncode != 0:
        return []
    seen: dict[str, None] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            seen.setdefault(line, None)
    return list(seen)


def resolved_comment_char(cwd: Path) -> str | None:
    """Resolve the repo's OWN comment char (§1 step 2, review Major 3). Default ``#``
    when ``core.commentChar`` is unset; a custom char (e.g. ``;``) is used as-is.

    ``auto`` is special-cased to ``None`` (round-2 corrected rationale): git resolves
    ``auto`` to a real character BEFORE this hook ever runs, but ``git config --get
    core.commentChar`` still echoes back the literal string ``"auto"`` -- the actually
    resolved character is UNKNOWABLE from here, and guessing risks unchosen candidate
    lines surviving as permanent commit content (measured: a ``commit.template`` with a
    leading ``#`` line makes git pick a non-``#`` char, so a ``#``-writing hook's lines
    survive uncommented). Degrade beats guessing -- callers must write nothing when this
    returns ``None``."""
    result = _run_git(["config", "--get", "core.commentChar"], cwd)
    value = result.stdout.strip() if result is not None and result.returncode == 0 else ""
    if not value:
        return "#"
    if value == "auto":
        return None
    return value


def open_index_ro(store_dir: Path) -> sqlite3.Connection | None:
    """Open ``<store_dir>/index.db`` strictly read-only, URI ``mode=ro&immutable=0``,
    with :data:`RO_OPEN_TIMEOUT_SECONDS` as the busy-timeout fallback (§0). ``None`` on
    ANY failure -- index absent, ro-open denied, or a concurrent writer's lock not
    released inside the timeout -- so every caller degrades to writing/resolving
    nothing rather than blocking or raising. Never rebuilds the index; never write-opens
    the store."""
    index_path = Path(store_dir) / "index.db"
    uri = f"file:{quote(str(index_path))}?mode=ro&immutable=0"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=RO_OPEN_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        # Force the lock check now (connect() itself doesn't touch the file), inside the
        # timeout window above, rather than deferring it silently to the caller's first
        # query.
        conn.execute("SELECT 1")
    except sqlite3.Error:
        return None
    return conn


# ============================================================================
# П1 -- sidegraph-prepare-commit-msg
# ============================================================================


@dataclass
class RecordCandidate:
    id: str
    kind: str  # "decision" | "fact"
    label: str  # title (decision) or statement (fact), already truncated


def _truncate(text: str, limit: int = LABEL_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _load_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _rule_a_candidates(conn: sqlite3.Connection, cwd: Path) -> dict[str, RecordCandidate]:
    """Rule (a): records (decisions AND facts -- П0) whose ``provenance.commit == HEAD``,
    plus the pre-П0 timestamp fallback for records with no commit stamp at all. Skipped
    entirely when there is no HEAD yet (empty repo, review M7)."""
    out: dict[str, RecordCandidate] = {}
    head = head_commit(cwd)
    if head is None:
        return out
    head_time = head_commit_time(cwd)
    for table, kind, label_field in (
        ("decisions", "decision", "title"),
        ("facts", "fact", "statement"),
    ):
        for row in conn.execute(f"SELECT data FROM {table}"):
            rec = _load_json(row["data"])
            if rec is None:
                continue
            provenance = rec.get("provenance") or {}
            commit = provenance.get("commit")
            matched = False
            if commit == head:
                matched = True
            elif commit is None and head_time is not None:
                valid_from_raw = rec.get("valid_from")
                if valid_from_raw:
                    try:
                        valid_from = datetime.fromisoformat(valid_from_raw)
                    except ValueError:
                        valid_from = None
                    if valid_from is not None and valid_from > head_time:
                        matched = True
            if matched:
                rid = rec.get("id")
                if rid:
                    out[rid] = RecordCandidate(
                        id=rid, kind=kind, label=_truncate(rec.get(label_field) or "")
                    )
    return out


def _rule_b_candidates(conn: sqlite3.Connection, cwd: Path) -> dict[str, RecordCandidate]:
    """Rule (b): staged-file bindings (review Major 4). The entity scan runs ONCE,
    bucketed by path -- never per staged file. Entities with no ``descriptor.file_path``
    (Tier-0/Tier-1, abstract/domain) never enter the bucket map at all, so their
    bindings are never even looked up -- they cannot consume the cap. Decisions-only
    (rule (b) never offers a fact): a matched binding whose ``record_id`` isn't in the
    ``decisions`` table is silently skipped."""
    out: dict[str, RecordCandidate] = {}
    paths = staged_files(cwd)
    if not paths:
        return out
    staged = set(paths)

    # ONE scan, bucketed by path -- filter to path-carrying entities FIRST.
    by_path: dict[str, list[str]] = {}
    for row in conn.execute("SELECT data FROM entities"):
        entity = _load_json(row["data"])
        if entity is None:
            continue
        descriptor = entity.get("descriptor") or {}
        file_path = descriptor.get("file_path")
        if not file_path:
            continue  # path-less (Tier-0/Tier-1) -- never enters the map
        entity_id = entity.get("entity_id")
        if entity_id:
            by_path.setdefault(file_path, []).append(entity_id)

    matched_entity_ids: set[str] = set()
    for p in staged:
        matched_entity_ids.update(by_path.get(p, []))
    if not matched_entity_ids:
        return out

    # Gather bindings ONLY from matched (path-carrying, staged) entities -- a Tier-0/1
    # binding was never fetched in the first place, so it can never consume the cap.
    best_weight: dict[str, float] = {}
    for entity_id in matched_entity_ids:
        for row in conn.execute(
            "SELECT data FROM anchor_bindings WHERE entity_id = ?", (entity_id,)
        ):
            binding = _load_json(row["data"])
            if binding is None:
                continue
            record_id = binding.get("record_id")
            if not record_id:
                continue
            weight = binding.get("weight", 1.0)
            if record_id not in best_weight or weight > best_weight[record_id]:
                best_weight[record_id] = weight

    # THEN cap -- filtering already happened above; this must never run before it.
    top_ids = sorted(best_weight, key=lambda rid: best_weight[rid], reverse=True)[
        :STAGED_CANDIDATE_CAP
    ]
    for record_id in top_ids:
        row = conn.execute("SELECT data FROM decisions WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            continue  # rule (b) is decisions-only
        decision = _load_json(row["data"])
        if decision is None:
            continue
        out[record_id] = RecordCandidate(
            id=record_id, kind="decision", label=_truncate(decision.get("title") or "")
        )
    return out


def collect_prepare_commit_candidates(
    store_dir: Path, cwd: Path, deadline: float
) -> list[RecordCandidate]:
    """Rules (a) + (b), deduped by id. Returns ``[]`` on every degrade condition (index
    absent/locked, no HEAD, no staged files, deadline exceeded) -- never raises."""
    conn = open_index_ro(store_dir)
    if conn is None:
        return []
    try:
        candidates: dict[str, RecordCandidate] = {}
        if time.monotonic() < deadline:
            candidates.update(_rule_a_candidates(conn, cwd))
        if time.monotonic() < deadline:
            for rid, cand in _rule_b_candidates(conn, cwd).items():
                candidates.setdefault(rid, cand)
        return list(candidates.values())
    finally:
        conn.close()


def render_candidate_block(candidates: list[RecordCandidate], char: str) -> str:
    lines = [f"{char} {_HEADER_LINE}"]
    for c in candidates:
        lines.append(f"{char} {TRAILER_KEY}: {c.id} — {c.label}")
    return "\n".join(lines) + "\n"


def apply_prepare_commit_msg(message_file: str, cwd: Path, store_dir: Path) -> None:
    """Write commented candidate trailers into ``message_file`` (§1). No-op (never
    raises, never blocks past :data:`HOOK_WALL_CLOCK_BUDGET_SECONDS`) when there is
    nothing to offer, the comment char can't be resolved (``auto``), or the deadline is
    already spent.

    The block is APPENDED, never prepended (review Major 1, measured against real git
    2.54, ``commit.cleanup=strip`` -- the default for an editor-invoked commit, exactly
    what a ``prepare-commit-msg`` hook firing with ``source`` absent implies). A
    prepended block puts the post-uncomment trailer line directly above the subject
    with no blank line between them; git's cleanup folds the two into ONE paragraph
    (the subject's own), and trailers are only ever recognized in the message's LAST
    paragraph -- so the documented "uncomment the line" gesture would silently produce
    a commit with no real trailer at all. Appending after the original content puts the
    (blank-line-separated) trailer paragraph last, where git actually looks."""
    deadline = time.monotonic() + HOOK_WALL_CLOCK_BUDGET_SECONDS
    candidates = collect_prepare_commit_candidates(store_dir, cwd, deadline)
    if not candidates or time.monotonic() >= deadline:
        return
    char = resolved_comment_char(cwd)
    if char is None:
        return  # "auto" -- unresolvable, never guess (see resolved_comment_char)
    if time.monotonic() >= deadline:
        return
    try:
        original = Path(message_file).read_text(encoding="utf-8")
    except OSError:
        return
    block = render_candidate_block(candidates, char)
    try:
        Path(message_file).write_text(original.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    except OSError:
        return


# ============================================================================
# П2 -- sidegraph-blame
# ============================================================================


@dataclass
class Hunk:
    start: int
    end: int
    sha: str


_BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$")


def blame_hunks(
    path: str, cwd: Path, line_range: tuple[int, int] | None = None
) -> list[Hunk] | None:
    """``git blame --porcelain [-L A,B] -- <path>`` -> merged contiguous-sha hunks.
    ``None`` on any git failure (not a repo, unknown file, git absent -- G9's CLI error
    surface)."""
    args = ["blame", "--porcelain"]
    if line_range is not None:
        args += ["-L", f"{line_range[0]},{line_range[1]}"]
    args += ["--", path]
    result = _run_git(args, cwd)
    if result is None or result.returncode != 0:
        return None
    entries: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        m = _BLAME_HEADER_RE.match(line)
        if m:
            entries.append((int(m.group(2)), m.group(1)))
    entries.sort(key=lambda e: e[0])
    hunks: list[Hunk] = []
    for final_line, sha in entries:
        if hunks and hunks[-1].sha == sha and hunks[-1].end == final_line - 1:
            hunks[-1].end = final_line
        else:
            hunks.append(Hunk(start=final_line, end=final_line, sha=sha))
    return hunks


def commit_date(sha: str, cwd: Path) -> str | None:
    result = _run_git(["log", "-1", "--format=%cI", sha], cwd)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def trailer_decision_ids(sha: str, cwd: Path) -> list[str]:
    """§2 step 2, trailer join -- corrected literal (review Major 2, verified against
    git 2.54): ``valueonly`` singular. The spec-rev-1 ``valuesonly`` is not a recognized
    git option and git echoes the literal placeholder string back instead of erroring --
    a silent-failure literal this exact spelling avoids.

    §1's rendered candidate line is ``Sidegraph-Decision: <id> — <label>`` -- the label
    is there to help a human pick which candidate to keep, not part of the trailer's
    real value, but a straight "uncomment the line" (the documented gesture) makes the
    literal trailer value ``"<id> — <label>"``, decorative suffix included (measured,
    end-to-end smoke). Each comma-separated value is trimmed to its leading token up to
    the first `` — `` / `` -- `` so the join still resolves whether or not a human
    bothered to hand-trim the label first."""
    result = _run_git(
        [
            "log",
            "-1",
            f"--format=%(trailers:key={TRAILER_KEY},valueonly,separator=%x2C)",
            sha,
        ],
        cwd,
    )
    if result is None or result.returncode != 0:
        return []
    raw = result.stdout.strip()
    if not raw:
        return []
    ids: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        part = re.split(r"\s+—\s+|\s+--\s+", part, maxsplit=1)[0].strip()
        if part:
            ids.append(part)
    return ids


@dataclass
class ResolvedRecord:
    id: str
    kind: str | None  # "decision" | "fact" | None (unresolved)
    label: str = ""
    superseded_by: str | None = None
    resolved: bool = True


def _all_records(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every decision + fact row, keyed by id, parsed once."""
    out: dict[str, dict] = {}
    for table in ("decisions", "facts"):
        for row in conn.execute(f"SELECT data FROM {table}"):
            rec = _load_json(row["data"])
            if rec is not None and rec.get("id"):
                rec["_kind"] = "decision" if table == "decisions" else "fact"
                out[rec["id"]] = rec
    return out


def _supersessions(records: dict[str, dict]) -> dict[str, str]:
    """predecessor id -> successor id, from every record's own ``supersedes`` field."""
    out: dict[str, str] = {}
    for rec in records.values():
        pred = rec.get("supersedes")
        if pred:
            out[pred] = rec["id"]
    return out


def records_by_commit(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """§2 step 2, provenance join -- decisions AND facts (post-П0) whose
    ``provenance.commit == sha``, bucketed once (not per hunk/sha)."""
    out: dict[str, list[dict]] = {}
    records = _all_records(conn)
    for rec in records.values():
        commit = (rec.get("provenance") or {}).get("commit")
        if commit:
            out.setdefault(commit, []).append(rec)
    return out


def resolve_sha(
    sha: str,
    cwd: Path,
    records: dict[str, dict],
    supersessions: dict[str, str],
    by_commit: dict[str, list[dict]],
) -> list[ResolvedRecord]:
    """Both join paths, deduped by id (§2 step 2/4): trailers first, then provenance-only
    additions. A resolved record whose id is now superseded prints its ``superseded_by``
    pointer (§2 step 4) -- blame surfaces history, the chain does the rest."""
    ids: dict[str, None] = {}
    for rid in trailer_decision_ids(sha, cwd):
        ids.setdefault(rid, None)
    for rec in by_commit.get(sha, []):
        ids.setdefault(rec["id"], None)

    out: list[ResolvedRecord] = []
    for rid in ids:
        resolved_rec = records.get(rid)
        if resolved_rec is None:
            out.append(ResolvedRecord(id=rid, kind=None, resolved=False))
            continue
        label_field = "title" if resolved_rec["_kind"] == "decision" else "statement"
        out.append(
            ResolvedRecord(
                id=rid,
                kind=resolved_rec["_kind"],
                label=_truncate(resolved_rec.get(label_field) or "", limit=80),
                superseded_by=supersessions.get(rid),
            )
        )
    return out


@dataclass
class BlameHunkResult:
    start: int
    end: int
    sha: str
    date: str | None
    records: list[ResolvedRecord]


def blame_report(
    path: str, cwd: Path, store_dir: Path, line_range: tuple[int, int] | None = None
) -> list[BlameHunkResult] | None:
    """The full П2 pipeline: git blame -> per-distinct-sha join (trailers + provenance)
    -> resolved records, in hunk (line-range) order -- the single entry point
    ``cli.blame_main`` calls; formatting/capping/JSON stay in ``cli.py``.

    ``None`` only when git blame itself fails (not a repo, unknown file/range, git
    missing -- G9's CLI error surface). A missing or lock-timed-out index degrades to
    every hunk resolving to nothing (``ResolvedRecord.resolved=False`` is never used
    here for that case -- an index-less run simply can't join at all, so every hunk's
    ``records`` list comes back empty) rather than a hard failure: blame without a store
    is still useful blame."""
    hunks = blame_hunks(path, cwd, line_range)
    if hunks is None:
        return None
    conn = open_index_ro(store_dir)
    records: dict[str, dict] = {}
    supersessions: dict[str, str] = {}
    by_commit: dict[str, list[dict]] = {}
    if conn is not None:
        try:
            records = _all_records(conn)
            supersessions = _supersessions(records)
            by_commit = records_by_commit(conn)
        finally:
            conn.close()
    date_cache: dict[str, str | None] = {}
    out: list[BlameHunkResult] = []
    for h in hunks:
        if h.sha not in date_cache:
            date_cache[h.sha] = commit_date(h.sha, cwd)
        resolved = resolve_sha(h.sha, cwd, records, supersessions, by_commit)
        out.append(
            BlameHunkResult(
                start=h.start, end=h.end, sha=h.sha, date=date_cache[h.sha], records=resolved
            )
        )
    return out
