"""CLI entry points: init (Stage 2 bootstrap), ratify (Stage 5), sync (Stage 6),
import (semantic-docs-layer wave, S2), domains (mind-model layer, M3), compact (git-native
store wave, N5), verify (CI-integrity wave, snapshot layer).

``sidegraph-init`` bootstraps ``.sidegraph/`` in a target repo and prints ready-to-paste
wiring. ``sidegraph-ratify`` lists proposed decisions AND facts human-readably (a
decision's still-proposed supporting facts nest under it; standalone facts get their own
section); ``--accept``/``--drop``/``--all`` apply the gestures to decision, fact, or domain
ids alike. Non-interactive flags keep it scriptable; drop is append-only (sets valid_to +
rejected, never deletes). ``sidegraph-sync`` re-anchors the store after a
Graphify rebuild; ``--json`` prints the report as one JSON object (``sync.report_as_dict``
-- the same shape the ``sync_anchors`` MCP tool returns) and ``--check`` exits 2 when
``sync.report_has_findings`` flags an error outcome, a stale decision, or a slug conflict
-- orphaned/ambiguous outcomes stay informational, a live-experiment refinement (design/
superpowers/specs/2026-07-11-ci-live-findings-design.md ruling 1: a legitimate rename+heal
must not red-flag forever). ``sidegraph-import`` bootstraps decisions from rationale nodes
(code AST + LLM docs) via ``importer.import_rationales``, or (with ``--docs``) from decision-shaped
markdown via ``doc_import.import_docs``. ``sidegraph-domains`` authors Domain proposals:
``bootstrap`` (from graph communities, via ``domains.bootstrap_domains``) and ``add`` (manual,
mirrors the ``add_domain`` MCP tool). ``sidegraph-compact`` packs terminal-status decisions/
domains into an immutable ``archive/`` segment via ``Store.compact`` — explicit, human-run
maintenance; never called by sync/retrieval/ratify. ``sidegraph-verify`` lints the store's
canonical files against the write-path invariants ``store.py`` enforces at write time
(``verify.verify_snapshot`` — pure read, no ``Store()``; see design/superpowers/specs/
2026-07-11-ci-integrity-design.md ruling 2); ``--json`` prints ``{"clean", "violations"}``,
exit 0 clean / 1 operational error (unreadable store dir) / 2 violations found. ``--against
<git-ref>`` additionally runs the transition layer (``verify.verify_against`` — git plumbing
via subprocess, classifying every store file changed vs ``<git-ref>`` against the store's OWN
write rules; the always-on snapshot layer still runs too, so violations from both layers are
reported together under the same exit contract) — an unresolvable ref or a store dir outside
any git repo is also an operational error (exit 1), never a violation. All return
non-zero on hard failures so CI can script them — see docs/guides/capturing-decisions.md
(ratify), docs/reference/cli.md#sidegraph-import (import),
docs/reference/cli.md#importing-decision-shaped-markdown---docs (--docs),
docs/concepts/mind-model.md (domains),
docs/reference/cli.md#sidegraph-compact (compact), and
docs/reference/cli.md#sidegraph-verify (verify).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from . import gitio
from .capture import (
    RatifyPolicy,
    format_domain_proposal,
    format_fact_proposal,
    format_proposal,
    parse_ratify_policy,
)
from .config import DEFAULT_STORE_DIR, resolve_store_path
from .doc_import import _MIN_SECTION_LIMIT, _SECTION_LIMIT, import_docs
from .doctor import curate
from .domains import DEFAULT_CANDIDATE_LIMIT, bootstrap_domains, collect_domain_candidates
from .engine.reader import GraphifyReader
from .host.claude_settings import (
    RATIFY_POLICY_DEFAULT,
    RATIFY_POLICY_ENV_VAR,
    SETTINGS_RELATIVE_PATH,
    RatifyPolicySettingsResult,
    current_ratify_policy,
    ensure_ratify_policy_setting,
    repo_root_for_settings,
)
from .importer import import_rationales
from .okf import build_bundle, write_bundle
from .profiles import PROFILES, get_profile
from .retrieval import TOC_CACHE_KEY, build_toc, proposal_surfaces
from .schema import Decision, DecisionKind, DecisionStatus, Domain, DomainStatus, Fact, Provenance
from .stats.model import UnreadableRecordError, build_report
from .stats.render import render_json, render_text
from .store import VOLATILE_STALE_KEY, Store
from .sync import (
    activate_accepted_domain,
    report_as_dict,
    report_has_findings,
    sync,
)
from .verify import verify_against, verify_snapshot
from .viz.model import build_graph
from .viz.render import to_html, to_json

# Above this count, a non-dry `sidegraph-domains bootstrap` run nags on stderr to
# reconsider --min-members/--limit rather than blindly ratifying everything — a big
# unfiltered batch is exactly the case where selective ratification matters most.
_BOOTSTRAP_LARGE_RUN_THRESHOLD = 50

# Shared ``--db`` help text (design §5): every subcommand below resolves the same way —
# through ``config.resolve_store_path`` — so they all document the same precedence.
# ``SIDEGRAPH_DIR`` is the primary knob; ``SIDEGRAPH_DB`` is kept for back-compat (a
# one-line deprecation notice prints to stderr the first time it's actually used).
_DB_HELP = (
    "store directory (default: $SIDEGRAPH_DIR if set, else existing "
    f"'{DEFAULT_STORE_DIR}/' if present, else '{DEFAULT_STORE_DIR}'; $SIDEGRAPH_DB is "
    "honored for back-compat, deprecated — a legacy *.db file path still works there via "
    "one-time migration)"
)

# ``sidegraph-stats`` cannot use the shared text: it reads ``<dir>/index.db`` and never opens a
# ``Store``, and migration happens when a ``Store`` opens. Handed a legacy ``*.db`` file it exits
# 2 looking for ``<file>/index.db``, so its help says what it does instead of what the others do.
_STATS_DB_HELP = (
    "store directory (default: $SIDEGRAPH_DIR if set, else existing "
    f"'{DEFAULT_STORE_DIR}/' if present, else '{DEFAULT_STORE_DIR}'; $SIDEGRAPH_DB is "
    "honored for back-compat, deprecated). This command only reads <directory>/index.db and "
    "never opens the store, so it cannot migrate a legacy *.db file: run a command that opens "
    "the store first (sidegraph-init)"
)


def _looks_already_initialized(path: Path) -> bool:
    """True iff ``path`` already names a real store: an existing legacy single-file store
    (a plain file — ``Store`` migrates it in place on open), a directory that already
    carries the canonical layout (its ``format`` marker exists — see
    ``store.Store._ensure_format_marker``), or a directory holding an UN-migrated legacy
    ``decisions.db`` (no ``format`` marker yet, since migration hasn't run — see
    ``store.Store._migrate_legacy``). That last case (review Minor 5) is real,
    pre-existing data: without it, ``init_main`` reported "created store" for a directory
    ``Store(...)`` was about to MIGRATE a moment later, understating what actually
    happened. A bare ``path.exists()`` used to false-positive on any pre-existing-but-empty
    directory too, reporting "already initialized" for a directory ``sidegraph-init`` was
    about to populate for the first time."""
    if path.is_file():
        return True
    if (path / "format").exists():
        return True
    return (path / "decisions.db").is_file()


def _report_ratify_policy_write(
    rel_settings: str, result: RatifyPolicySettingsResult, value: str
) -> None:
    """Print the outcome of one `ensure_ratify_policy_setting` call for `init_main` --
    shared by the interactive-answer path and the `--ratify-policy` flag path, both of
    which already know `value` (whatever they decided to write) before calling this."""
    if result.outcome == "written":
        print(f"wrote {rel_settings}: env.{RATIFY_POLICY_ENV_VAR}={value}")
        # The MCP server reads the policy once, from the environment it started with; a
        # server already running keeps proposing under the old value.
        print(
            "  restart your Claude Code session so the Sidegraph MCP server picks it up: "
            "a running server keeps the environment it started with"
        )
    elif result.outcome == "already_set":
        print(
            f"{rel_settings} already sets "
            f"{RATIFY_POLICY_ENV_VAR}={result.existing_value}. Left unchanged."
        )
    else:
        print(
            f"{rel_settings} exists but isn't valid JSON/an object. Left untouched, "
            f'add this yourself: "env": {{"{RATIFY_POLICY_ENV_VAR}": "{value}"}}'
        )


def _ask_ratify_policy_interactively(rel_settings: str) -> str:
    """Ask once, interactively, whether this project should auto-ratify low-risk records
    -- the owner-specified alternative to a silent write (whitepaper claim C-075: the
    self-certification risk of an automatic policy is unmeasured, so it must not become a
    project's setting without a person answering for it).

    A bare Enter or a recognized "yes" returns ``RATIFY_POLICY_DEFAULT`` ("auto-low-risk");
    a recognized "no" returns "manual" -- both are answers the caller then writes into
    ``rel_settings`` explicitly, so the project's choice is committed either way. An
    unrecognized answer re-asks once with a short reminder; a second unrecognized answer
    (or an EOF, e.g. stdin closing mid-prompt) falls back to the default without asking a
    third time -- a settings prompt must never be able to hang or fail init.
    """
    print(
        "Auto-ratify low-risk records? Lessons, gotchas, and standalone facts are accepted "
        "the moment they're written, as long as they anchor to real code. Architecture "
        "decisions, constraints, and domains still wait for a person either way. The "
        f"answer is stored in {rel_settings}, committed with the project, and changeable "
        "later."
    )
    prompts = ["Enable it? [Y/n] ", "Please answer y or n. Enable it? [Y/n] "]
    for prompt in prompts:
        try:
            raw = input(prompt)
        except EOFError:
            break
        normalized = raw.strip().lower()
        if normalized in ("", "y", "yes"):
            return RATIFY_POLICY_DEFAULT
        if normalized in ("n", "no"):
            return "manual"
    return RATIFY_POLICY_DEFAULT


def init_main(argv: list[str] | None = None) -> int:
    """Bootstrap a target repo: create the store, check for the graph, print wiring.

    Idempotent: safe to re-run on an already-initialized repo (says so, exits 0). The graph
    is optional at init time — a missing ``graph.json`` is reported, not an error, since
    ``sidegraph-init`` typically runs before the first ``graphify update .``.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-init",
        description="Bootstrap Sidegraph in a repo: create the store and print wiring snippets.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="store directory to create (default: $SIDEGRAPH_DIR if set, else existing "
        f"'{DEFAULT_STORE_DIR}/' if present, else '{DEFAULT_STORE_DIR}' — the recommended "
        "in-repo location; $SIDEGRAPH_DB is honored for back-compat, deprecated. Passing a "
        "legacy *.db file path still works via one-time migration)",
    )
    parser.add_argument(
        "--graph",
        default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json"),
        help="graphify-out/graph.json path to check for (never created here; read-only input)",
    )
    settings_flags = parser.add_mutually_exclusive_group()
    settings_flags.add_argument(
        "--no-settings",
        action="store_true",
        help=(
            f"skip {SETTINGS_RELATIVE_PATH.as_posix()} entirely: no prompt, no write, "
            "just print the line to add by hand"
        ),
    )
    settings_flags.add_argument(
        "--ratify-policy",
        choices=[p.value for p in RatifyPolicy],
        default=None,
        help=(
            f"set {RATIFY_POLICY_ENV_VAR} in {SETTINGS_RELATIVE_PATH.as_posix()} to this "
            "value with no prompt (never overwrites an existing value there) -- for a "
            "scripted, non-interactive setup that still wants an explicit answer"
        ),
    )
    args = parser.parse_args(argv)
    # Resolved AFTER parse_args, and only when --db was actually omitted (review Minor 4):
    # resolve_store_path() can print SIDEGRAPH_DB's one-line deprecation notice as a side
    # effect, which must never fire just from BUILDING the parser -- e.g. on `--help`, or
    # on a run that passes --db explicitly and was never going to consult the env at all.
    if args.db is None:
        args.db = resolve_store_path(warn_on_create=False)

    db_path = Path(args.db)
    already_initialized = _looks_already_initialized(db_path)
    try:
        Store(args.db)  # creates parent dir(s) + schema-stamped store; idempotent itself
    except Exception as e:
        print(f"store not writable ({db_path}): {e}")
        return 1

    if already_initialized:
        print(f"already initialized: {db_path} exists.")
    else:
        print(f"created store: {db_path}")

    graph_path = Path(args.graph)
    if graph_path.exists():
        print(f"found graph: {graph_path}")
    else:
        print(
            f"missing graph: {graph_path} — run `graphify update .`, then `sidegraph-sync`, "
            "to anchor decisions to code (optional; Sidegraph works without it)."
        )

    rel_settings = SETTINGS_RELATIVE_PATH.as_posix()
    if args.no_settings:
        export_value = RATIFY_POLICY_DEFAULT
        print(
            f"skipped {rel_settings} (--no-settings). Set it yourself: "
            f'"env": {{"{RATIFY_POLICY_ENV_VAR}": "{export_value}"}}'
        )
    elif args.ratify_policy is not None:
        export_value = args.ratify_policy
        _report_ratify_policy_write(
            rel_settings,
            ensure_ratify_policy_setting(repo_root_for_settings(), export_value),
            export_value,
        )
    else:
        state = current_ratify_policy(repo_root_for_settings())
        if state.outcome == "already_set":
            export_value = state.existing_value or RATIFY_POLICY_DEFAULT
            print(
                f"{rel_settings} already sets "
                f"{RATIFY_POLICY_ENV_VAR}={state.existing_value}. Left unchanged."
            )
        elif state.outcome == "skipped":
            export_value = RATIFY_POLICY_DEFAULT
            print(
                f"{rel_settings} exists but isn't valid JSON/an object. Left untouched, "
                f'add this yourself: "env": {{"{RATIFY_POLICY_ENV_VAR}": "{export_value}"}}'
            )
        elif sys.stdin.isatty():
            export_value = _ask_ratify_policy_interactively(rel_settings)
            _report_ratify_policy_write(
                rel_settings,
                ensure_ratify_policy_setting(repo_root_for_settings(), export_value),
                export_value,
            )
        else:
            # No TTY: CI, a script, an agent-driven session -- nobody is there to answer,
            # so a silent write is exactly the self-certification risk the owner rejected
            # (whitepaper claim C-075). Write nothing, ask nothing, just say how.
            export_value = RATIFY_POLICY_DEFAULT
            print(
                f"non-interactive: leaving {rel_settings} untouched. To auto-ratify "
                f'low-risk records, add "env": {{"{RATIFY_POLICY_ENV_VAR}": '
                f'"{export_value}"}} yourself, or re-run sidegraph-init from a terminal.'
            )
    print(
        f"Codex or another host without {rel_settings}: "
        f"export {RATIFY_POLICY_ENV_VAR}={export_value}"
    )

    print()
    print("Wire up Claude Code — the plugin installs the MCP server and all three hooks")
    print("(SessionStart, Stop, PreToolUse) automatically:")
    print()
    print("  /plugin marketplace add SantyagoSeaman/sidegraph")
    print("  /plugin install sidegraph@sidegraph")
    print()
    print("Prefer no plugin? Register just the MCP server (writes a repo-committed")
    print(".mcp.json, so teammates get it via git):")
    print()
    print(
        "  claude mcp add sidegraph -s project "
        "--env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json "
        "-- uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main "
        "sidegraph-mcp"
    )
    print()
    print("Hook-by-hook manual setup and Codex CLI wiring: see")
    print("docs/getting-started/claude-code-setup.md and docs/getting-started/codex-setup.md.")
    return 0


def _route_ratify(store: Store, id_: str, action: str) -> tuple[str, list[Fact]]:
    """Route one id to a decision, a fact, or a domain by lookup (decision first, then
    fact, then domain) and apply ``action`` ("accept" | "drop"); unknown ids get a generic
    error. Mirrors ``server._ratify_one``'s routing — including its ``(result, cascaded)``
    return shape (``cascaded`` is the list of facts that rode a DECISION's verdict in this
    call; always empty for a fact/domain id) — so ``sidegraph-ratify`` covers all three
    kinds without importing ``server.py`` — that used to matter more than it does now:
    server.py's old module-level ``_store = Store(...)`` created a stray store as a side
    effect of merely importing it, but ``server._get_store()`` is a lazy, memoized accessor
    now (see ``config.py``), so that particular hazard is gone. This CLI still avoids the
    import to stay clear of server.py's FastMCP app entirely — a much heavier dependency
    than a script needs — so the seam is kept even though the original hazard no longer
    forces it.
    """
    if store.get_decision(id_) is not None:
        try:
            if action == "accept":
                _decision, cascaded = store.ratify(id_)
                return "accepted", cascaded
            _decision, cascaded = store.drop(id_)
            return "dropped", cascaded
        except ValueError as e:
            return f"error: {e}", []
    if store.get_fact(id_) is not None:
        try:
            if action == "accept":
                store.ratify_fact(id_)
            else:
                store.drop_fact(id_)
            return ("accepted" if action == "accept" else "dropped"), []
        except ValueError as e:
            return f"error: {e}", []
    if store.get_domain(id_) is not None:
        result = (
            store.ratify_domains(accept=[id_])
            if action == "accept"
            else store.ratify_domains(drop=[id_])
        )
        return result[id_], []
    return f"error: unknown id {id_!r} (not a pending decision, fact, or domain)", []


def _nested_fact_ids(store: Store, proposals: list[Decision]) -> set[str]:
    """Ids of still-``PROPOSED`` facts that support one of ``proposals`` -- these ride
    their decision's ratify verdict (cascade) and are rendered nested under that decision
    in the queue, never listed again in the standalone "Facts:" section. Mirrors
    ``server._nested_fact_ids`` (duplicated rather than imported, same rationale as
    ``_route_ratify``'s docstring) — shared here by the bare-run render and ``--all``'s id
    collection so neither double-counts a cascaded fact."""
    return {
        f.id
        for d in proposals
        for f in store.facts_for_decision(d.id)
        if f.status == DecisionStatus.PROPOSED
    }


_NOT_SURFACING = "[not surfacing]"


def _format_proposed_decision_block(store: Store, d: Decision) -> str:
    """``format_proposal(d)`` plus one indented ``  evidence: ...`` line per still-
    ``PROPOSED`` fact supporting it -- mirrors ``server._format_proposed_decision_block``
    (duplicated, same rationale as ``_route_ratify``'s docstring) so the bare-run render
    stays pixel-identical to ``list_proposed``'s."""
    # Round-2 practitioner review: mark what has already stopped being delivered. The
    # surfacing window is only humane if the queue says which items it has stopped
    # serving — otherwise a reviewer cannot tell an urgent backlog from an inert one, and
    # the window reads as a silent drop. The record stays listed and ratifiable either way.
    head = format_proposal(d)
    if not proposal_surfaces(d):
        # On the TITLE line, where the eye lands — not at the end of a multi-line block.
        first, _, rest = head.partition("\n")
        head = f"{first}  {_NOT_SURFACING}" + (f"\n{rest}" if rest else "")
    lines = [head]
    for f in store.facts_for_decision(d.id):
        if f.status == DecisionStatus.PROPOSED:
            lines.append(f"  evidence: {f.statement} [{f.source}]  ({f.id})")
    return "\n".join(lines)


def _ratified_domain(store: Store, id_: str, result: str) -> bool:
    """True iff ``id_`` was routed to (and actually landed on) a domain — gates the TOC
    cache refresh below to real domain changes only (mirrors ``server._ratified_domain``;
    duplicated rather than imported so this CLI stays clear of ``server.py``'s module-level
    store, same rationale as ``_route_ratify``'s docstring)."""
    if result.startswith("error"):
        return False
    return store.get_decision(id_) is None and store.get_domain(id_) is not None


def _graph_path_for_store(graph_path: str | Path, db_path: str | Path) -> Path:
    """``graph_path`` as an absolute path: a RELATIVE one is resolved against the STORE's own
    project root, not the process CWD. Shared by every command that takes a store and a
    ``--graph`` together (``sidegraph-ratify``, ``sidegraph-stats``): a store in another
    directory would otherwise pick up whatever ``graphify-out/graph.json`` happened to sit
    beside the shell, pairing one project's records with a different project's graph.

    The project root is the store path's PARENT. Only a legacy single-FILE store living
    inside a store directory needs one more level up. A name check on ".sidegraph" was tried
    and rejected (review R2-4): ``--db mystore`` is a supported invocation, and a name check
    resolved such a store's root to itself, silently disabling this for anyone not on the
    default directory name — the exact "dead while looking alive" failure this helper exists
    to avoid."""
    path = Path(graph_path)
    if path.is_absolute():
        return path
    store = Path(db_path).resolve()
    root = store.parent
    if not store.is_dir() and (root.name == ".sidegraph" or (root / "format").is_file()):
        root = root.parent
    return root / path


def _ratify_reader(graph_path: str, db_path: str | Path):
    """A best-effort reader for ratify's immediate membership resolve (CLI/MCP parity,
    v0.2-scope). ``None`` when there is no readable graph — ratify must still work in a
    checkout that has never been built, exactly as it did before this existed, so every
    failure here degrades to the pre-existing "schedule the heal" branch rather than
    failing the accept.

    A RELATIVE ``--graph`` is resolved against the STORE's own project root (see
    :func:`_graph_path_for_store`). Pinned by
    ``test_cli_ratify_ignores_a_graph_belonging_to_another_project``, which builds its own
    graph in a foreign directory and chdirs there — the pre-existing
    ``test_cli_ratify_of_an_accepted_domain_schedules_a_heal`` also catches it, but only
    when a gitignored ``graphify-out/graph.json`` happens to exist in the checkout, so it
    is not a guard CI can rely on (review finding 5)."""
    path = _graph_path_for_store(graph_path, db_path)
    if not path.is_file():
        return None
    try:
        return GraphifyReader(str(path))
    except Exception:
        return None


def ratify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sidegraph-ratify",
        description="Review and ratify Sidegraph decisions and domains awaiting acceptance.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    parser.add_argument("--accept", nargs="*", default=[], metavar="ID")
    parser.add_argument("--drop", nargs="*", default=[], metavar="ID")
    parser.add_argument(
        "--all", action="store_true", help="accept every pending proposal (decisions AND domains)"
    )
    parser.add_argument(
        "--graph",
        default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json"),
        help=(
            "graph to resolve an accepted domain's membership against, immediately, the "
            "way the MCP ratify tool does (CLI/MCP parity). Read-only input, same meaning "
            "as every other subcommand's --graph. A relative path resolves against the "
            "STORE's project root, not the shell's CWD. Missing or unreadable is fine: the "
            "accept still lands and the membership heal is scheduled for the next sync."
        ),
    )
    args = parser.parse_args(argv)
    args.db = resolve_store_path(args.db)

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    if args.all:
        proposals = list(store.iter_proposed())
        args.accept = [d.id for d in proposals]
        args.accept += [d.domain_id for d in store.iter_domains(status=DomainStatus.PROPOSED)]
        # Standalone proposed facts only -- a fact with a proposed supporter rides that
        # decision's cascade already; adding it here too would double-report it.
        nested = _nested_fact_ids(store, proposals)
        args.accept += [f.id for f in store.iter_proposed_facts() if f.id not in nested]

    if not args.accept and not args.drop:
        proposals = list(store.iter_proposed())
        domains = list(store.iter_domains(status=DomainStatus.PROPOSED))
        nested = _nested_fact_ids(store, proposals)
        standalone_facts = [f for f in store.iter_proposed_facts() if f.id not in nested]
        if not proposals and not domains and not standalone_facts:
            print("No proposed decisions, facts, or domains pending ratification.")
        else:
            printed = False
            if proposals:
                print("Decisions:")
                print("\n\n".join(_format_proposed_decision_block(store, d) for d in proposals))
                printed = True
            if standalone_facts:
                if printed:
                    print()
                print("Facts:")
                print("\n\n".join(format_fact_proposal(f) for f in standalone_facts))
                printed = True
            if domains:
                if printed:
                    print()
                print("Domains:")
                print("\n\n".join(format_domain_proposal(d) for d in domains))
                printed = True
            print(
                f"\n{len(proposals) + len(domains) + len(standalone_facts)} pending. "
                "Use --accept ID... / --drop ID... / --all."
            )
        return 0

    had_error = False
    domain_changed = False
    domain_accepted = False  # accept-side only -- drops need no membership resolution
    accepted_domain_ids: list[str] = []  # the ones whose membership must resolve now
    accepted: dict[str, str] = {}

    def _process_accept(did: str) -> None:
        # Task 7 fix pass, Important-1: shared by both accept passes below so a
        # decision-id's own line and its cascade lines print together, in whichever pass
        # actually routes it.
        nonlocal domain_changed, domain_accepted, had_error
        result, cascaded = _route_ratify(store, did, "accept")
        accepted[did] = result
        if _ratified_domain(store, did, result):
            domain_changed = True
            domain_accepted = True
            accepted_domain_ids.append(did)
        if result.startswith("error"):
            detail = result.split(":", 1)[1].strip() if ":" in result else result
            print(f"error {did}: {detail}")
            had_error = True
        else:
            print(f"{result} {did}")
        for f in cascaded:
            accepted[f.id] = f"accepted (evidence of {did})"
            print(f"accepted (evidence of {did}) {f.id}")

    # Pass 1: every decision id in --accept first, regardless of its position in the list
    # (mirrors server._ratify_impl's order-independence fix) -- a fact nested under one of
    # these decisions must always be swept by ITS cascade, never independently re-ratified
    # first just because it was listed earlier. Pass 2 then skips any id pass 1 already
    # reported via cascade: no error line, no had_error, no re-processing a record that's
    # already been flipped.
    for did in args.accept:
        if did in accepted or store.get_decision(did) is None:
            continue
        _process_accept(did)
    for did in args.accept:
        if did in accepted:
            continue
        _process_accept(did)
    dropped: dict[str, str] = {}
    # Fix pass, Important-2 (CLI-only -- server._ratify_impl's drop loop already had this
    # guard from its accept/drop cross-list dedup): `--drop <d> <nested-f>` (f supports d)
    # used to print the cascade line for f, then hit f's own turn and re-route it --
    # `drop_fact` now raises "fact ... is not proposed" (f is already REJECTED), printing a
    # spurious "error" line and forcing exit 1 despite full success. No two-pass reordering
    # here (unlike the accept loop) -- drop ids are dropped in the caller's own list order;
    # this guard only skips an id ALREADY reported earlier in that same order, via cascade.
    for did in args.drop:
        if did in dropped:
            continue
        result, cascaded = _route_ratify(store, did, "drop")
        dropped[did] = result
        domain_changed = domain_changed or _ratified_domain(store, did, result)
        if result.startswith("error"):
            detail = result.split(":", 1)[1].strip() if ":" in result else result
            print(f"error {did}: {detail}")
            had_error = True
        else:
            print(f"{result} {did}")
        for f in cascaded:
            dropped[f.id] = f"dropped (evidence of {did})"
            print(f"dropped (evidence of {did}) {f.id}")
    # Fix: lazy sync alone keeps last_synced_graph_version current without ever recomputing
    # the TOC cache, so "bootstrap -> ratify -> SessionStart TOC comes alive" did nothing
    # until the next real graph rebuild. Rebuild immediately whenever >= 1 domain id was
    # actually accepted/dropped by this run; a decisions-only ratify leaves it untouched.
    if domain_changed:
        store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))
    if domain_accepted:
        # CLI/MCP ratify parity (v0.2-scope): resolve membership NOW, the way the MCP
        # `ratify` tool does, so the same accept through two doors leaves the same state —
        # a domain accepted in the shell used to show `communities: []` until the next
        # sync. sync.activate_accepted_domain (design D2 shared helper) resolves each
        # domain individually and schedules the VOLATILE_STALE_KEY heal itself whenever it
        # cannot (no reader, or the refresh raises) -- never fails this ratify either way.
        # This loop just tracks whether EVERY domain resolved (a redundant but harmless
        # belt-and-suspenders flag set below) and renders the "path rule too broad"
        # sentence, which the helper deliberately leaves to its callers (a literal trigger
        # phrase in the sidegraph:heal-anchors skill).
        reader = _ratify_reader(args.graph, args.db)
        all_resolved = bool(accepted_domain_ids)
        for domain_id in accepted_domain_ids:
            domain = store.get_domain(domain_id)
            if domain is None:
                all_resolved = False
                continue
            # One domain failing must not strand the others unresolved (review 6b: a
            # `break` left later domains with communities: [] until the next sync, which
            # is the very asymmetry this parity fix closes) -- the helper isolates each
            # domain's own try/except, so this loop just keeps going regardless.
            activation = activate_accepted_domain(domain, store, reader)
            if not activation.resolved:
                all_resolved = False
            if activation.overbroad is not None:
                # Same sentence the MCP path prints — "path rule too broad" is a literal
                # trigger phrase in the sidegraph:heal-anchors skill, so dropping it costs
                # the CLI user the routed heal (review 6a).
                prefixes = ", ".join(repr(pfx) for pfx in domain.path_prefixes)
                print(
                    f"  path rule too broad: {prefixes} match "
                    f"{activation.overbroad['matched']}/{activation.overbroad['total']} "
                    "communities — not applied; seed_anchors, if any, still applied"
                )
        if not all_resolved:
            store.set_meta(VOLATILE_STALE_KEY, "1")
    return 1 if had_error else 0


def sync_main(argv: list[str] | None = None) -> int:
    """Re-anchor the decision store against the current graph (post-commit or on demand).

    ``--json`` prints ``sync.report_as_dict(report)`` as one JSON object to stdout --
    nothing else on stdout -- the same shape the ``sync_anchors`` MCP tool returns.
    ``--check`` exits 2 when ``sync.report_has_findings`` flags an attention finding (an
    error outcome, a stale decision, a slug conflict, or a domain refresh failure); exit 0
    is a clean report (``orphaned``/``ambiguous`` outcomes and ``empty_domains``/
    ``overbroad_domains`` stay informational, never fail the check on their own -- a
    legitimate rename+heal leaves the renamed-away entity's leaf orphaned for good, no
    retirement path in an append-only store, and that residue must not red-flag forever;
    when an orphaned/ambiguous anchor actually costs reachability, the record goes stale
    and ``stale_decisions`` already fires). ``--check`` implies ``force=True`` -- a check
    that could be silently pre-empted by an earlier caller (SessionStart, a retrieval MCP
    call) consuming the one-shot cold-reload heal and discarding its report would be worse
    than one that always pays the rebind ladder's cost, so ``--check`` never reads a
    version-skip as "clean" the way a plain sync legitimately can. The two flags compose
    (``--json --check``); the operational-error exit-1 paths below are unchanged. See
    design/superpowers/specs/2026-07-11-ci-live-findings-design.md ruling 1.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-sync",
        description="Re-resolve entity anchors after a Graphify rebuild.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    parser.add_argument(
        "--graph", default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    )
    parser.add_argument("--force", action="store_true", help="rerun even if version matches")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as one JSON object to stdout (nothing else) — same shape "
        "as the sync_anchors MCP tool",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 2 when the report has an attention finding (error outcome, stale "
        "decision, or slug conflict — orphaned/ambiguous outcomes stay informational) "
        "— for CI",
    )
    args = parser.parse_args(argv)
    args.db = resolve_store_path(args.db)

    try:
        reader = GraphifyReader(args.graph)
    except Exception as e:
        print(f"graph not readable ({args.graph}): {e}")
        return 1

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    try:
        # --check implies force=True: a check that can be silently pre-empted by an
        # earlier caller (SessionStart, a retrieval MCP call) consuming the one-shot
        # cold-reload heal and discarding its report is worse than one that always pays
        # the rebind ladder's cost. See docs/reference/cli.md's --check section.
        report = sync(store, reader, force=args.force or args.check)
    except Exception as e:
        print(f"sync failed: {e}")
        return 1

    report_dict = report_as_dict(report)

    if args.json:
        # Pure JSON on stdout — nothing else — so a CI step can pipe this straight into
        # jq/json.load without stripping prose first.
        print(json.dumps(report_dict, indent=2))
    elif report.skipped:
        print(f"up to date (graph {report.to_version})")
    else:
        print(
            f"synced {report.from_version or '<never>'} -> {report.to_version}: "
            f"{report.counts() or 'no tracked entities'}"
        )
        total_repointed = sum(o.repointed for o in report.outcomes)
        if total_repointed:
            print(f"re-pointed {total_repointed} community binding(s)")
        if report.domains_refreshed:
            print(f"refreshed community mapping for {report.domains_refreshed} domain(s)")
        for o in report.outcomes:
            if o.status in ("moved", "moved_uncommitted", "orphaned", "ambiguous", "error"):
                print(f"  {o.status}: {o.canonical_name} ({o.detail or 'no match'})")
        if report.stale_decisions:
            print("possibly stale decisions (all anchors gone — verify):")
            for d in report.stale_decisions:
                print(f"  {d['id']}  {d['title']}")
        if report.empty_domains:
            print("possibly empty domains — re-scope or supersede:")
            for dom in report.empty_domains:
                print(f"  {dom['slug']}  {dom['title']}")
        if report.overbroad_domains:
            print(
                "path rule too broad — path contribution dropped (seed_anchors, if any, "
                "still applied); narrow path_prefixes or re-scope:"
            )
            for dom in report.overbroad_domains:
                print(
                    f"  {dom['slug']}  {dom['title']}  "
                    f"({dom['matched']}/{dom['total']} communities)"
                )
        if report.domain_failures:
            print("domains that FAILED to refresh (fix, then re-run with --force):")
            for dom in report.domain_failures:
                print(f"  {dom['slug']}  {dom['title']}  ({dom['error']})")
        if report.slug_conflicts:
            print("slug conflicts — drop one (sidegraph-ratify --drop <loser-id>):")
            for c in report.slug_conflicts:
                ids = ", ".join(c["domain_ids"])
                print(
                    f"  slug conflict: {c['slug']!r} held by {len(c['domain_ids'])} live "
                    f"domains ({ids}) — drop one"
                )

    if args.check and report_has_findings(report_dict):
        return 2
    return 0


def _import_docs_mode(args: argparse.Namespace) -> int:
    """``sidegraph-import --docs ...`` — decision-shaped markdown -> anchored decisions
    (importer #2, ``doc_import.import_docs``). Split out from ``import_main`` purely for
    readability; shares every flag except ``--docs``/``--tag``/``--section-limit``
    (docs-only) and ``--path`` (rationale-mode-only, rejected here — see ``import_main``).

    Existence of every ``--docs`` path is checked BEFORE opening the store/graph (same "no
    I/O side effect on a rejected flag" ordering as the negative-``--limit``/
    ``--section-limit`` guards in ``import_main``) — a typo'd path must not create an empty
    store next to the real one.

    ``--profile`` selects the reader dialect and default ingest globs — resolved before any
    store/graph I/O, so an unknown profile name is a clean exit-1; with no explicit PATH, its
    ingest globs are expanded (relative to the current directory) into repo-relative paths.
    """
    profile_name = args.profile or "generic-adr"
    try:
        profile = get_profile(profile_name)
    except ValueError as e:
        print(str(e))
        return 1

    # A bare `--docs` (no PATH, legal now that --profile can drive discovery instead) still
    # appends `None` to the list via nargs="?" — filter those out to find any REAL explicit
    # paths; guards `args.docs is None` too (flag omitted entirely, --profile-only mode).
    explicit_docs = [p for p in args.docs if p is not None] if args.docs is not None else []
    if explicit_docs:
        docs_paths: list[str] = explicit_docs
        missing = [p for p in docs_paths if not Path(p).exists()]
        if missing:
            print(f"--docs path(s) not found: {', '.join(missing)}")
            return 1
    else:
        # Profile-only: discover files via the profile's ingest globs, relative to cwd.
        # Profile globs are repo-relative; keep the expanded paths repo-relative too, so
        # provenance.ref / the "imported from" context are deterministic across machines and
        # dedup against an equivalent relative `--docs <path>` run (see cross-invocation
        # idempotency test). Path.cwd().glob(...) always yields paths under cwd, so
        # relative_to(Path.cwd()) never raises.
        docs_paths = sorted(
            str(p.relative_to(Path.cwd()))
            for glob in profile.ingest_globs
            for p in Path.cwd().glob(glob)
        )

    args.db = resolve_store_path(args.db)

    try:
        reader = GraphifyReader(args.graph)
    except Exception as e:
        print(f"graph not readable ({args.graph}): {e}")
        return 1

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    # Resolved once here, immediately before the batch call (design D1) — one parse per
    # CLI invocation, never re-read mid-import. Hoisted to a local (not inline in the
    # kwargs dict below) so the result-line printer further down can test it too, with no
    # second env read (Ruling O).
    ratify_policy: RatifyPolicy = parse_ratify_policy(os.environ.get("SIDEGRAPH_RATIFY_POLICY"))
    # `kind=None` -> import_docs auto-detects per document (B4); `section_limit` only
    # overridden when the user actually passed --section-limit, else import_docs's own
    # default (_SECTION_LIMIT) applies.
    import_docs_kwargs: dict = dict(
        kind=args.kind,
        propose=args.propose,
        dry_run=args.dry_run,
        limit=args.limit,
        tags=args.tag,
        profile=profile_name,
        any_doc=args.any_doc,
        ratify_policy=ratify_policy,
    )
    if args.section_limit is not None:
        import_docs_kwargs["section_limit"] = args.section_limit
    report = import_docs(store, reader, docs_paths, **import_docs_kwargs)

    # BUG F: an absolute `--docs` path is resolved relative to the current working directory
    # to match the graph's repo-relative `source_file`, so running `sidegraph-import` from
    # anywhere but the repo root makes every doc-node anchor miss — yielding a silent
    # "0 imported, N unanchorable". When (almost) every anchor-attempted doc came back
    # unanchorable AND an absolute path was passed, that footgun is the likeliest cause:
    # warn loudly on stderr instead of leaving the empty result unexplained. Guarded on an
    # absolute path being present so the common case (relative paths from the repo root) is
    # byte-identical and never triggers a spurious warning.
    anchor_attempted = report.imported + report.superseded + report.skipped_unanchorable
    if (
        report.skipped_unanchorable > 0
        and anchor_attempted > 0
        and report.skipped_unanchorable / anchor_attempted >= 0.5
        and any(os.path.isabs(p) for p in docs_paths)
    ):
        print(
            f"warning: {report.skipped_unanchorable} doc(s) unanchorable — if you passed an "
            "absolute --docs path, run sidegraph-import from the repo root (doc paths are "
            "resolved relative to the current directory, so they only match graph.json's "
            "root-relative source_file entries when the current directory IS the repo root).",
            file=sys.stderr,
        )

    if args.dry_run:
        for item in report.dry_run:
            # `ref` is the effective ref (`path` or `path#fragment` for a split child) —
            # the per-record identity; `file_path` stays the real path for by_file()'s
            # per-document rollup (oneshot+granularity spec, review F14/F4).
            print(f"{item['ref']}: [{item['action']}] {item['title']}")
            for skip in item["anchors_skipped"]:
                print(f"    anchor skipped: {skip['name']} ({skip['reason']})")
        print(
            f"\nwould import {report.imported} decision(s), supersede {report.superseded} "
            f"(skipped: {report.skipped_existing} existing, "
            f"{report.skipped_unanchorable} unanchorable, "
            f"{report.skipped_not_decision} not-decision-shaped, "
            f"{report.skipped_unparseable} unparseable, "
            f"{report.skipped_superseded_frontmatter} superseded-frontmatter, "
            f"{report.skipped_outside_profile} outside-profile)"
        )
        if report.status_derived_rejected:
            print(f"{report.status_derived_rejected} would land rejected (source status: rejected)")
        if report.status_derived_proposed:
            print(
                f"{report.status_derived_proposed} would land proposed "
                "(source status: draft/proposed/pending/under review)"
            )
        if report.skipped_template:
            print(f"{report.skipped_template} skipped as template(s) (not decisions)")
        if report.skipped_degenerate_parent:
            print(
                f"{report.skipped_degenerate_parent} split parent(s) skipped as degenerate "
                "(echoed context or empty choice) — children imported on their own"
            )
        for fp, n in sorted(report.by_file().items()):
            print(f"  {fp}: {n}")
        # D4 (design/superpowers/specs/2026-09-23-doc-import-encoding-design.md): printed
        # LAST. The by_file lines share the block's two-space indent, so any printed after
        # the block would read as more undecodable files.
        if report.skipped_undecodable:
            print(
                f"{report.skipped_undecodable} file(s) skipped: not valid UTF-8, re-save as "
                "UTF-8 to import:"
            )
            for fp in report.undecodable_files:
                print(f"  {fp}")
        return 0

    # Design D6: the count is added to the non-dry-run summary line only, only when the
    # resolved policy is not `manual` — the dry-run line above (`report.dry_run`'s own
    # printer) is left untouched.
    auto_segment = (
        f", auto-ratified {report.auto_ratified}" if ratify_policy != RatifyPolicy.MANUAL else ""
    )
    print(
        f"imported {report.imported} decision(s), superseded {report.superseded}{auto_segment} "
        f"(skipped: {report.skipped_existing} existing, "
        f"{report.skipped_unanchorable} unanchorable, "
        f"{report.skipped_not_decision} not-decision-shaped, "
        f"{report.skipped_unparseable} unparseable, "
        f"{report.skipped_superseded_frontmatter} superseded-frontmatter, "
        f"{report.skipped_outside_profile} outside-profile)"
    )
    if report.status_derived_rejected:
        print(f"{report.status_derived_rejected} landed rejected (source status: rejected)")
    if report.status_derived_proposed:
        print(
            f"{report.status_derived_proposed} landed proposed "
            "(source status: draft/proposed/pending/under review)"
        )
    if report.skipped_template:
        print(f"{report.skipped_template} skipped as template(s) (not decisions)")
    if report.skipped_degenerate_parent:
        print(
            f"{report.skipped_degenerate_parent} split parent(s) skipped as degenerate "
            "(echoed context or empty choice) — children imported on their own"
        )
    # D4 (design/superpowers/specs/2026-09-23-doc-import-encoding-design.md): printed last
    # on stdout, after the degenerate-parent line — the auto-ratify-failures loop below
    # goes to stderr, so this stays the last stdout line either way.
    if report.skipped_undecodable:
        print(
            f"{report.skipped_undecodable} file(s) skipped: not valid UTF-8, re-save as "
            "UTF-8 to import:"
        )
        for fp in report.undecodable_files:
            print(f"  {fp}")
    for entry in report.auto_ratify_failures:
        print(f"auto-ratify failure: {entry}", file=sys.stderr)
    return 0


def import_main(argv: list[str] | None = None) -> int:
    """Bootstrap decisions either from Graphify rationale nodes (default), or — with
    ``--docs`` — from decision-shaped markdown (importer #2, ``doc_import.import_docs``;
    see docs/reference/cli.md#importing-decision-shaped-markdown---docs).

    Default mode reads rationale nodes via the reader, writes through
    ``importer.import_rationales`` (redact -> validate -> anchor -> write). ``--dry-run``
    lists what would be imported and writes nothing; ``--limit``/``--path`` bound the volume
    on large repos (the test corpus yields 492 rationale nodes) — docs recommend
    ``--dry-run`` first. A negative ``--limit`` is a hard usage error (exit 1), same
    convention as the readability checks below. See
    ``docs/reference/cli.md#sidegraph-import``.

    ``--docs PATH`` (repeatable; each a file or a directory recursed for ``*.md``) switches
    to importer #2 instead of adding to importer #1's rationale scan — ``--path`` (the
    rationale-mode graph-prefix filter) is meaningless with ``--docs`` and combining them is
    a hard usage error (exit 1); ``--tag``/``--section-limit`` (docs-only) are simply unused
    in rationale mode. Every other flag (``--db``/``--graph``/``--kind``/``--propose``/
    ``--dry-run``/``--limit``) is shared.

    ``--kind`` (fix-wave B, B4) defaults to auto in ``--docs`` mode: per document, a
    qualifying "Root cause" section suggests ``lesson``, else ``adr`` — pass ``--kind``
    explicitly (any value, including ``adr``) to pin every document in the run to one kind,
    same as before B4. Rationale mode is unaffected: an omitted ``--kind`` there still means
    plain ``adr`` for every rationale, unconditionally.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-import",
        description="Bootstrap decisions from Graphify rationale nodes (code AST + LLM docs), "
        "or decision-shaped markdown (--docs).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    parser.add_argument(
        "--graph", default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    )
    parser.add_argument(
        "--kind",
        choices=[k.value for k in DecisionKind],
        default=None,
        help="Decision kind to assign (default: adr for rationale mode; --docs mode "
        "auto-detects lesson for a doc with a qualifying 'Root cause' section, else adr — "
        "pass explicitly to pin every document to one kind)",
    )
    parser.add_argument(
        "--propose",
        action="store_true",
        help="write as proposed (ratify gate) instead of accepted by default",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be imported; write nothing",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    parser.add_argument(
        "--path",
        action="append",
        default=None,
        metavar="PREFIX",
        help="only import rationales whose file_path starts with PREFIX (repeatable); "
        "rationale mode only, incompatible with --docs",
    )
    parser.add_argument(
        "--docs",
        nargs="?",
        action="append",
        default=None,
        metavar="PATH",
        help="import decision-shaped markdown instead of rationale nodes: PATH is a file or "
        "a directory (recursed for *.md); repeatable. Bare (no PATH) imports the active "
        "profile's ingest globs instead of explicit paths — generic-adr's by default, or "
        "the profile named by --profile.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help="flow-profile selecting the reader dialect and default ingest globs (--docs mode; "
        f"one of: {', '.join(sorted(PROFILES))}). With no explicit PATH, "
        "the profile's ingest globs (relative to the current directory) are used.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=None,
        metavar="TAG",
        help="tag every imported/superseded decision with a durable tag:<slug> entity "
        "(repeatable); --docs mode only",
    )
    parser.add_argument(
        "--section-limit",
        type=int,
        default=None,
        metavar="N",
        help="per-field char cap for context/choice/rejected/consequences, applied after "
        f"redaction at a word boundary (default: {_SECTION_LIMIT}; min {_MIN_SECTION_LIMIT}); "
        "--docs mode only",
    )
    parser.add_argument(
        "--any-doc",
        action="store_true",
        help="import any enumerated *.md file regardless of the active profile's "
        "ingest_globs (default: files outside the profile's declared scope are skipped and "
        "counted as outside-profile — --docs mode only)",
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 0:
        print(f"--limit must be >= 0 (got {args.limit})")
        return 1
    if args.section_limit is not None and args.section_limit < _MIN_SECTION_LIMIT:
        print(f"--section-limit must be >= {_MIN_SECTION_LIMIT} (got {args.section_limit})")
        return 1

    if args.docs is not None or args.profile is not None:
        if args.path is not None:
            print(
                "--docs/--profile and --path are mutually exclusive (--path is the "
                "rationale-mode graph-prefix filter; it has no meaning against markdown files)"
            )
            return 1
        return _import_docs_mode(args)

    args.db = resolve_store_path(args.db)

    try:
        reader = GraphifyReader(args.graph)
    except Exception as e:
        print(f"graph not readable ({args.graph}): {e}")
        return 1

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    # Rationale mode ignores --docs-only flags (--tag/--section-limit) and, unlike --docs
    # mode's auto-kind (B4), keeps its own unconditional "adr when omitted" default.
    # Resolved once here, immediately before the batch call (design D1) — one parse per
    # CLI invocation, never re-read mid-import.
    ratify_policy: RatifyPolicy = parse_ratify_policy(os.environ.get("SIDEGRAPH_RATIFY_POLICY"))
    report = import_rationales(
        store,
        reader,
        kind=args.kind or DecisionKind.ADR.value,
        propose=args.propose,
        dry_run=args.dry_run,
        limit=args.limit,
        path_prefixes=args.path,
        ratify_policy=ratify_policy,
    )

    if args.dry_run:
        for item in report.dry_run:
            print(f"{item['file_path'] or item['node_id']}: {item['title']}")
        print(
            f"\nwould import {report.imported} decision(s) "
            f"(skipped: {report.skipped_existing} existing, "
            f"{report.skipped_unanchorable} unanchorable, {report.filtered} filtered)"
        )
        for fp, n in sorted(report.by_file().items()):
            print(f"  {fp}: {n}")
        return 0

    auto_segment = (
        f", auto-ratified {report.auto_ratified}" if ratify_policy != RatifyPolicy.MANUAL else ""
    )
    print(
        f"imported {report.imported} decision(s){auto_segment} "
        f"(skipped: {report.skipped_existing} existing, "
        f"{report.skipped_unanchorable} unanchorable)"
    )
    for entry in report.auto_ratify_failures:
        print(f"auto-ratify failure: {entry}", file=sys.stderr)
    return 0


def _limit_truncation_note(effective_limit: int | None, total_before_limit: int) -> str | None:
    """The shared stderr line for "``--limit`` (default or explicit) cut off candidates
    this run never even considered" (finding B/C) — used both by the real run's before-
    write pre-check and by ``--dry-run``'s listing, so the wording never drifts between the
    two. ``None`` when nothing was actually truncated (``effective_limit`` unset, i.e. the
    "all" convention, or the full significant count already fit under it)."""
    if effective_limit is None or total_before_limit <= effective_limit:
        return None
    return (
        f"note: {total_before_limit} significant communities found — showing only the top "
        f"{effective_limit} (community-id order); pass --limit 0 for the full list, a "
        "higher --limit N, or narrow with --min-members/--paths"
    )


def _domains_bootstrap(args: argparse.Namespace) -> int:
    """``sidegraph-domains bootstrap`` — propose Domains from graph communities (§4.1).

    Validation happens BEFORE ``resolve_store_path``/``Store(...)`` (same ordering as
    ``import_main``'s ``--limit`` guard) so a rejected flag never creates a store as a
    side effect.

    ``--limit`` defaults to ``DEFAULT_CANDIDATE_LIMIT`` (100, finding B) — the same
    scale-aware default the ``list_domain_candidates`` MCP tool applies, so the two never
    disagree about what "a normal run" writes. ``--limit 0`` is the "all" convention
    (mirrors the tool's ``limit=0``), translated to the collector's own ``None``
    (unlimited) before it reaches ``bootstrap_domains``.

    BUG C: on a non-dry-run, the over-threshold/truncation nags are computed from a
    SEPARATE, read-only ``collect_domain_candidates`` pre-check and printed BEFORE
    ``bootstrap_domains`` writes a single record — never after, which is what let a real
    run flood the store with thousands of proposed-domain records before the old nag
    (computed from the already-finished report) ever printed.
    """
    if args.min_members < 1:
        print(f"--min-members must be >= 1 (got {args.min_members})")
        return 1
    if args.limit is not None and args.limit < 0:
        print(f"--limit must be >= 0 (got {args.limit})")
        return 1

    args.db = resolve_store_path(args.db)

    try:
        reader = GraphifyReader(args.graph)
    except Exception as e:
        print(f"graph not readable ({args.graph}): {e}")
        return 1

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    effective_limit = None if args.limit == 0 else args.limit

    if not args.dry_run:
        _, precheck_stats = collect_domain_candidates(
            store, reader, min_members=args.min_members, paths=args.paths, limit=effective_limit
        )
        note = _limit_truncation_note(effective_limit, precheck_stats.total_before_limit)
        if note is not None:
            print(note, file=sys.stderr)
        if precheck_stats.total > _BOOTSTRAP_LARGE_RUN_THRESHOLD:
            print(
                f"note: about to propose {precheck_stats.total} domains — consider "
                "--min-members/--limit and ratify selectively",
                file=sys.stderr,
            )

    # Resolved once here, immediately before the batch call (design D1) — one parse per
    # CLI invocation, never re-read mid-bootstrap.
    ratify_policy: RatifyPolicy = parse_ratify_policy(os.environ.get("SIDEGRAPH_RATIFY_POLICY"))
    report = bootstrap_domains(
        store,
        reader,
        min_members=args.min_members,
        paths=args.paths,
        dry_run=args.dry_run,
        limit=effective_limit,
        ratify_policy=ratify_policy,
    )

    if args.dry_run:
        for item in report.dry_run:
            print(f"{item['community_id']}: {item['slug']} — {item['title']}")
            for w in item["warnings"]:
                print(f"    warning: {w}")
        print(
            f"\nwould propose {report.proposed} domain(s) "
            f"(skipped: {report.skipped_existing} existing, "
            f"{report.below_threshold} below threshold, {report.filtered} filtered)"
        )
        note = _limit_truncation_note(effective_limit, report.total_before_limit)
        if note is not None:
            print(note, file=sys.stderr)
        return 0

    auto_segment = (
        f", auto-ratified {report.auto_ratified}" if ratify_policy != RatifyPolicy.MANUAL else ""
    )
    print(
        f"proposed {report.proposed} domain(s){auto_segment} "
        f"(skipped: {report.skipped_existing} existing)"
    )
    for entry in report.warnings:
        for w in entry["warnings"]:
            print(f"  warning ({entry['slug']}): {w}")
    for failure in report.auto_ratify_failures:
        print(f"auto-ratify failure: {failure}", file=sys.stderr)
    return 0


def _domains_add(args: argparse.Namespace) -> int:
    """``sidegraph-domains add`` — manually propose a Domain (§4.3, manual path); mirrors
    ``server._add_domain_impl`` (always lands ``status=proposed`` — manual authoring is not
    an exception to the ratification gate).

    A slug collision against a *live* (proposed|accepted) domain is reported as a skip
    (exit 0), same "expected outcome, not a hard failure" treatment as bootstrap's
    idempotency skip — only an unreadable store, an unresolvable ``--parent``, or an
    invalid draft (e.g. non-kebab-case ``--slug``) is a hard failure (exit 1). Draft shape
    is validated BEFORE any store is opened (same "no I/O side effect on a rejected flag"
    ordering as ``import_main``'s ``--limit`` guard) — a bad ``--slug`` must not create an
    empty store next to the real one.
    """
    try:
        Domain(
            slug=args.slug,
            title=args.title,
            summary=args.summary,
            path_prefixes=args.path or [],
            provenance=Provenance(source="manual"),
        )
    except ValidationError as e:
        print(f"error: {e}")
        return 1

    args.db = resolve_store_path(args.db)

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    parent_id = None
    if args.parent is not None:
        parent = store.find_domain_by_slug(args.parent)
        if parent is None:
            print(f"error: --parent {args.parent!r} does not resolve to any domain")
            return 1
        parent_id = parent.domain_id

    domain = Domain(
        slug=args.slug,
        title=args.title,
        summary=args.summary,
        parent_id=parent_id,
        path_prefixes=args.path or [],
        provenance=Provenance(source="manual"),
    )
    try:
        store.add_domain(domain)
    except ValueError as e:
        print(f"proposed 0 domain(s) (skipped: 1 existing — {e})")
        return 0

    print("proposed 1 domain(s) (skipped: 0 existing)")
    print(f"{domain.domain_id}  {domain.slug}")
    return 0


def domains_main(argv: list[str] | None = None) -> int:
    """``sidegraph-domains`` — author Domain proposals (mind-model layer; see
    docs/concepts/mind-model.md). Two subcommands:
    ``bootstrap`` (path 1, from graph communities) and ``add`` (path 3, manual). Both land
    ``status=proposed`` — ``sidegraph-ratify`` is the one gate for every authoring path.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-domains",
        description="Author Domain proposals: bootstrap from graph communities, or add manually.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="Propose domains from graph communities.")
    boot.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    boot.add_argument(
        "--graph", default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json")
    )
    boot.add_argument(
        "--min-members",
        type=int,
        default=5,
        metavar="N",
        help="minimum anchorable member count for a community to be proposed (default: 5)",
    )
    boot.add_argument(
        "--paths",
        action="append",
        default=None,
        metavar="PREFIX",
        help="only consider communities with a member whose file_path starts with PREFIX "
        "(repeatable)",
    )
    boot.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        metavar="N",
        help=f"cap the number of significant communities considered, top-N by community id "
        f"(default: {DEFAULT_CANDIDATE_LIMIT}; 0 = unlimited, the full list); must be >= 0",
    )
    boot.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be proposed; write nothing",
    )

    add = sub.add_parser("add", help="Manually propose a domain.")
    add.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    add.add_argument("--slug", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--summary", required=True)
    add.add_argument("--parent", default=None, metavar="SLUG")
    add.add_argument(
        "--path",
        action="append",
        default=None,
        metavar="PREFIX",
        help="path_prefixes stabilizer/bootstrap membership rule (repeatable)",
    )

    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        return _domains_bootstrap(args)
    return _domains_add(args)


def compact_main(argv: list[str] | None = None) -> int:
    """``sidegraph-compact`` — pack terminal-status (superseded/rejected/deprecated
    decisions; superseded/dropped domains) into an immutable
    ``archive/<date>-<seq>-<hash12>.jsonl`` segment via ``Store.compact`` and remove their
    now-redundant hot canonical files (see
    ``docs/reference/store-format.md#archive-segments-sidegraph-compact`` — the ``<hash12>``
    content-hash suffix exists so two branches compacting on the same day never collide on
    filename). Explicit, human-run maintenance: recommended on the default branch; never
    wired into sync/retrieval/ratify.

    ``--older-than N`` additionally requires N days in a terminal state (a decision's
    ``valid_to``; domains have no such field at all and are conservatively excluded —
    reported separately as "terminal age unknown" — whenever this flag is set; see
    ``Store.compact``'s docstring). ``--dry-run`` lists what would be compacted (and what
    leftover hot files from an interrupted prior run would be cleaned up) without writing
    or removing anything.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-compact",
        description="Pack terminal-status decisions and domains into an immutable archive "
        "segment, freeing them from the git-committed record-per-file directories. "
        "Append-only: records MOVE to the archive, never lost or mutated.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="N",
        help="only compact records that have been in a terminal state for at least N days "
        "(uses a decision's valid_to; domains carry no terminal timestamp at all and are "
        "conservatively excluded — kept hot — whenever this flag is set)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be compacted; write nothing",
    )
    args = parser.parse_args(argv)

    if args.older_than is not None and args.older_than < 0:
        print(f"--older-than must be >= 0 (got {args.older_than})")
        return 1

    args.db = resolve_store_path(args.db)

    try:
        store = Store(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    report = store.compact(older_than_days=args.older_than, dry_run=args.dry_run)

    if args.dry_run:
        for item in report.items:
            print(f"{item.ulid}  {item.kind}  {item.status}  {item.title}")
        if (
            not report.items
            and report.skipped_age_filtered == 0
            and report.domains_excluded_age_unknown == 0
            and report.cleaned_up_hot_files == 0
        ):
            print("nothing to compact")
            return 0
        print(
            f"\nwould compact {report.total_compacted} record(s) "
            f"({report.decisions_compacted} decisions, {report.domains_compacted} domains); "
            f"{report.skipped_age_filtered} skipped (not terminal enough)"
        )
        if report.domains_excluded_age_unknown:
            print(f"{report.domains_excluded_age_unknown} domain(s) excluded: terminal age unknown")
        if report.cleaned_up_hot_files:
            print(
                f"would also remove {report.cleaned_up_hot_files} leftover hot file(s) "
                "already durably archived by a prior, interrupted compact run"
            )
        return 0

    if report.total_compacted == 0 and report.cleaned_up_hot_files == 0:
        parts = []
        if report.skipped_age_filtered:
            parts.append(f"{report.skipped_age_filtered} skipped (not terminal enough)")
        if report.domains_excluded_age_unknown:
            parts.append(
                f"{report.domains_excluded_age_unknown} domain(s) excluded: terminal age unknown"
            )
        if parts:
            print("nothing to compact; " + "; ".join(parts))
        else:
            print("nothing to compact")
        return 0

    if report.segment_path:
        print(
            f"compacted {report.total_compacted} record(s) into {report.segment_path} "
            f"({report.decisions_compacted} decisions, {report.domains_compacted} domains); "
            f"{report.skipped_age_filtered} skipped (not terminal enough)"
        )
    else:
        print(f"compacted 0 record(s); {report.skipped_age_filtered} skipped (not terminal enough)")
    if report.domains_excluded_age_unknown:
        print(f"{report.domains_excluded_age_unknown} domain(s) excluded: terminal age unknown")
    if report.cleaned_up_hot_files:
        print(
            f"cleaned up {report.cleaned_up_hot_files} leftover hot file(s) from an "
            "interrupted prior compact run"
        )
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    """``sidegraph-verify`` — store integrity lint (design/superpowers/specs/
    2026-07-11-ci-integrity-design.md ruling 2). Two layers, combined into one report:

    - **Snapshot layer** (always runs): reads the store's canonical files directly
      (``verify.verify_snapshot`` — pure read, no ``Store()``, so a lint run can never
      itself write to ``index.db`` or migrate a legacy store) and reports every schema/
      referential-integrity violation found.
    - **Transition layer** (``--against <git-ref>``, additive): ``verify.verify_against``
      classifies every store file that changed vs ``<git-ref>`` against the store's OWN
      write rules (git plumbing via subprocess — ``git diff``/``git show``). Its violations
      are appended to the snapshot layer's, under the same report and exit contract.

    Unlike every other subcommand here, a missing/unreadable store directory is NOT
    auto-created: ``verify_snapshot`` raises, and that is the operational-error exit path
    (1) — a lint has nothing to lint if there is nothing to open, and silently creating an
    empty store just to report "clean" would be actively misleading in CI. Likewise,
    ``--against`` on a store dir outside any git repository, or against an unresolvable
    ``<git-ref>``, is also an operational error (exit 1) — never a violation (design ruling
    2: "Not-a-git-repo / unknown ref -> operational error").

    ``--json`` prints ``{"clean": bool, "violations": [{"code", "path", "detail"}, ...]}`` as
    one JSON object to stdout — nothing else — same "pure JSON on stdout" convention as
    ``sidegraph-sync --json``. Human output (the default) is one line per violation,
    ``<code>  <path>  <detail>``, then a one-line summary.

    Exit contract: 0 clean, 1 operational error (unreadable store dir / bad git ref), 2
    violations found.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-verify",
        description="Lint the decision store's canonical files against its write-path "
        "invariants (schema, validity windows, supersedes chains, referential integrity, "
        "ULID uniqueness across hot files and archive segments); --against additionally "
        "checks that every store file changed vs a git ref was mutated legally.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    parser.add_argument(
        "--against",
        default=None,
        metavar="GIT_REF",
        help="also run the transition layer: classify every store file changed vs GIT_REF "
        "against the store's own write rules (git diff/git show; CI mode)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print {'clean', 'violations'} as one JSON object to stdout (nothing else)",
    )
    args = parser.parse_args(argv)
    args.db = resolve_store_path(args.db, warn_on_create=False)

    try:
        violations = verify_snapshot(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1

    if args.against is not None:
        try:
            violations = violations + verify_against(args.db, args.against)
        except Exception as e:
            print(f"verify --against {args.against!r} failed: {e}")
            return 1

    if args.json:
        # Pure JSON on stdout — nothing else — mirrors sidegraph-sync --json.
        print(
            json.dumps(
                {
                    "clean": not violations,
                    "violations": [
                        {"code": v.code, "path": v.path, "detail": v.detail} for v in violations
                    ],
                },
                indent=2,
            )
        )
    else:
        for v in violations:
            print(f"{v.code}  {v.path}  {v.detail}")
        if violations:
            print(f"{len(violations)} violation(s)")
        else:
            print("clean")

    return 2 if violations else 0


def doctor_main(argv: list[str] | None = None) -> int:
    """``sidegraph-doctor`` — one-stop store health: strict verify + advisory curation.

    See design/superpowers/specs/2026-07-23-sidegraph-doctor-design.md. Composes the two
    halves rather than reimplementing either:

    - **Strict section**: ``verify.verify_snapshot`` (+ ``verify.verify_against`` when
      ``--against`` is given) — exactly what ``sidegraph-verify`` runs, same
      operational-error rules (a missing store is never auto-created; a bad git ref or a
      store outside any git repo is exit 1, never a violation).
    - **Advisory section**: ``doctor.curate`` — curation findings that never gate by
      default; ``--check`` escalates them to exit 2, mirroring ``sidegraph-sync
      --check``. A skipped check (no usable index.db) affects neither exit code nor
      cleanliness, even under ``--check``.

    ``--json`` prints ``{"clean", "violations", "findings", "skipped"}`` as one JSON
    object to stdout — nothing else (the sidegraph-verify/-sync convention). ``clean``
    is true only when violations AND findings are both empty. Exit contract: 0 healthy
    (advisory findings alone stay 0 without ``--check``), 1 operational error (including
    a negative ``--stale-days``, rejected before the store is touched — same hard-usage
    treatment as ``sidegraph-import --limit``), 2 strict violations or, with ``--check``,
    advisory findings.
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-doctor",
        description="Store health: sidegraph-verify's strict snapshot (+ optional "
        "--against transition layer) plus advisory curation lint (dangling records, "
        "decayed bindings, stale proposals, unreferenced entities, expired-but-open "
        "validity). Advisory findings never fail the run unless --check.",
    )
    parser.add_argument("--db", default=None, help=_DB_HELP)
    parser.add_argument(
        "--against",
        default=None,
        metavar="GIT_REF",
        help="also run verify's transition layer vs GIT_REF (same semantics as "
        "sidegraph-verify --against)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="advisory findings also exit 2 (default: report only; skipped checks never fail)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=30,
        metavar="N",
        help="stale-proposal threshold: flag proposals strictly older than N days (default 30)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print {'clean','violations','findings','skipped'} as one JSON object to "
        "stdout (nothing else)",
    )
    parser.add_argument(
        "--graph",
        default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json"),
        help="graph.json path, used only for the graph-root-mismatch advisory check "
        "(read-only; missing or unreadable simply skips that one check)",
    )
    args = parser.parse_args(argv)
    if args.stale_days < 0:
        # Reject before touching the store — a bad flag must not affect anything.
        print(f"--stale-days must be >= 0, got {args.stale_days}")
        return 1
    args.db = resolve_store_path(args.db, warn_on_create=False)

    try:
        violations = verify_snapshot(args.db)
    except Exception as e:
        print(f"store not readable ({args.db}): {e}")
        return 1
    if args.against is not None:
        try:
            violations = violations + verify_against(args.db, args.against)
        except Exception as e:
            print(f"doctor --against {args.against!r} failed: {e}")
            return 1

    try:
        reader: GraphifyReader | None = GraphifyReader(args.graph)
    except Exception:
        # Same "missing graph is fine" convention every other command with a --graph
        # default uses — the graph-root-mismatch check just doesn't run.
        reader = None

    report = curate(args.db, stale_days=args.stale_days, reader=reader)

    if args.json:
        # Pure JSON on stdout — nothing else — mirrors sidegraph-verify --json.
        print(
            json.dumps(
                {
                    "clean": not violations and not report.findings,
                    "violations": [
                        {"code": v.code, "path": v.path, "detail": v.detail} for v in violations
                    ],
                    "findings": [
                        {"code": f.code, "path": f.path, "detail": f.detail}
                        for f in report.findings
                    ],
                    "skipped": report.skipped,
                },
                indent=2,
            )
        )
    else:
        for v in violations:
            print(f"{v.code}  {v.path}  {v.detail}")
        for f in report.findings:
            print(f"{f.code}  {f.path}  {f.detail}")
        for name in report.skipped:
            print(f"{name} check skipped (no usable index.db — run sidegraph-sync)")
        # Ratification latency (2026-08-04 lifecycle D4 follow-up, practitioner
        # resolution 1.2): median/max ratified_at − valid_from over records that carry
        # the stamp. Pre-stamp records are excluded, never guessed; no stamped records →
        # no line. Informational only: never a finding, never affects the exit code, and
        # deliberately absent from --json (whose key set is a pinned contract).
        try:
            from .doctor import _iter_records, _parse_aware_iso

            stamps = []
            for subdir in ("decisions", "facts", "domains"):
                for _path, rec in _iter_records(Path(args.db), subdir):
                    # D5: auto stamps excluded -- ratified_at ~= valid_from for those
                    # would collapse the median toward 0 and it would stop measuring
                    # human queue latency.
                    if isinstance(rb := rec.get("ratified_by"), str) and rb.startswith("auto:"):
                        continue
                    ra = _parse_aware_iso(rec.get("ratified_at"))
                    vf = _parse_aware_iso(rec.get("valid_from"))
                    if ra is not None and vf is not None:
                        stamps.append((ra - vf).total_seconds() / 86400)
            latencies = sorted(stamps)
            if latencies:
                median = latencies[len(latencies) // 2]
                print(
                    f"time-to-ratify: median {median:.0f} days, max {latencies[-1]:.0f} "
                    f"days ({len(latencies)} stamped record(s))"
                )
        except Exception:
            pass  # a stat failure must never cost the health report
        # Auto-ratification audit (design D5, 2026-09-11): auto share of ever-ratified
        # records and their later-retired rate vs. human, per kind -- the early-warning
        # metric for a hallucinating auto-ratify loop. Informational only: never a
        # finding, never affects the exit code, deliberately absent from --json (key set
        # pinned, tests/test_cli_doctor.py:118-127). Printed only when at least one
        # auto: stamp exists anywhere (hot plus archive) -- a manual deployment's output
        # is otherwise byte-identical (D4: "manual deployments see zero render diff").
        try:
            from .doctor import _auto_share_lines

            for line in _auto_share_lines(args.db):
                print(line)
        except Exception:
            pass  # a stat failure must never cost the health report
        if violations or report.findings:
            print(f"{len(violations)} violation(s), {len(report.findings)} finding(s)")
        else:
            print("clean")

    if violations:
        return 2
    if args.check and report.findings:
        return 2
    return 0


def viz_main(argv: list[str] | None = None) -> int:
    """``sidegraph-viz`` — render a read-only graph of the owned decision/fact store.

    Writes ``<out>.html`` (a self-contained, offline interactive vis-network page) and
    ``<out>.json`` (the same ``{nodes, edges, stats}`` shape). Nodes are decisions, facts, and
    the entities they anchor to; anchor edges are colored by status (live/degraded/orphaned),
    plus supersede chains and fact->decision (supports) links. Diagnostic footer counts
    orphaned/degraded bindings and dangling (unanchored) records.

    ``--json`` prints the graph JSON to stdout and writes no file. ``--only-problems`` keeps
    only the subgraph touching a degraded/orphaned binding or a dangling record.
    ``--no-superseded`` omits terminal-status records (shown dimmed by default). Truncation
    past ``--max-nodes`` is warned on stderr and recorded in ``stats.truncated`` — never
    silent. Exit 0 on success (a store WITH problems still exits 0 — the problems are the
    output), 2 on an operational error (uninitialized store / unwritable output).

    # see design/superpowers/specs/2026-07-12-decision-graph-viz-design.md
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-viz",
        description="Render a read-only interactive graph of the decision/fact store.",
    )
    parser.add_argument("--db", default=None, help=_DB_HELP)
    parser.add_argument(
        "--out",
        default="sidegraph-graph",
        help="output base path; writes <out>.html and <out>.json (default: sidegraph-graph)",
    )
    parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="open the written HTML in the default browser",
    )
    parser.add_argument(
        "--only-problems",
        action="store_true",
        help="only the subgraph touching a degraded/orphaned binding or a dangling record",
    )
    parser.add_argument(
        "--no-superseded",
        dest="include_superseded",
        action="store_false",
        help="omit superseded/rejected/deprecated records (default: shown, dimmed)",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=800,
        help="cap node count; excess is truncated (lowest-priority first) and reported",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the graph JSON to stdout (nothing else) and write no HTML",
    )
    args = parser.parse_args(argv)
    args.db = resolve_store_path(args.db, warn_on_create=False)

    if not _looks_already_initialized(Path(args.db)):
        print(
            f"no store at {args.db} — run `sidegraph-init` first",
            file=sys.stderr,
        )
        return 2

    store = Store(args.db)
    graph = build_graph(
        store,
        include_superseded=args.include_superseded,
        only_problems=args.only_problems,
        max_nodes=args.max_nodes,
    )

    if args.json:
        print(json.dumps(to_json(graph), indent=2))
        return 0

    out_html = Path(f"{args.out}.html")
    out_json = Path(f"{args.out}.json")
    try:
        out_html.write_text(to_html(graph), encoding="utf-8")
        out_json.write_text(json.dumps(to_json(graph), indent=2), encoding="utf-8")
    except OSError as e:
        print(f"cannot write output ({args.out}): {e}", file=sys.stderr)
        return 2

    st = graph.stats
    print(
        f"wrote {out_html} ({st.decisions} decisions, {st.facts} facts, "
        f"{st.entities} entities; {st.orphaned_bindings} orphaned, "
        f"{st.degraded_bindings} degraded, {st.dangling_records} dangling)"
    )
    if st.truncated:
        print(
            f"warning: truncated {st.truncated} node(s) past --max-nodes={args.max_nodes}",
            file=sys.stderr,
        )
    if args.open_browser:
        webbrowser.open(out_html.resolve().as_uri())
    return 0


def stats_main(argv: list[str] | None = None) -> int:
    """``sidegraph-stats`` — one screen of local usage statistics.

    Read-only. Reports how often memory was asked for in the window, how much of the code
    being worked in carries memory, what the store holds, and anchor health. It states what
    was shown, asked and touched — never what was improved or prevented (spec D2).

    ``--json`` prints the serialized report and nothing else; a figure the text withholds
    (recording off, no render journal, no readable graph) is ``null`` there, never a zero.
    Exit 0 on success (a silent
    store still exits 0 — the silence is the output), 2 on an operational error: a bad
    ``--window``, no store index (never created here), an index SQLite cannot read, or a
    record row in it that does not parse (named by table and record id). An
    index from before the render journal, and a graph that is missing or unreadable, are
    not errors — they are stated in the report.

    # see design/superpowers/specs/2026-09-18-usage-stats-design.md
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-stats",
        description="One screen of local usage statistics: how often memory was asked for, "
        "how much of the code worked in carries it, and what the store holds.",
    )
    parser.add_argument("--db", default=None, help=_STATS_DB_HELP)
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        metavar="DAYS",
        help="days of journal to report on (default 30, the journal's retention)",
    )
    parser.add_argument(
        "--graph",
        default=os.environ.get("SIDEGRAPH_GRAPH", "graphify-out/graph.json"),
        help="graph.json path (read-only; missing or unreadable is reported, not an error). "
        "A relative path resolves against the store's project root, not the shell's directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as one JSON object to stdout (nothing else)",
    )
    args = parser.parse_args(argv)

    # Reject before touching any path — a bad flag must not affect anything. The subtraction
    # is the same one `build_report` makes, so a window it would overflow on is refused here
    # rather than surfacing as a traceback from inside the aggregator.
    try:
        if args.window <= 0:
            raise ValueError
        datetime.now(UTC) - timedelta(days=args.window)
    except (ValueError, OverflowError):
        print(f"--window must be a positive number of days, got {args.window}", file=sys.stderr)
        return 2

    store_dir = Path(resolve_store_path(args.db, warn_on_create=False))
    index = store_dir / "index.db"
    if not index.is_file():
        print(f"no store index at {index} — run `sidegraph-init` first", file=sys.stderr)
        return 2

    # A relative --graph is the STORE's project's, not the shell's (same rule as ratify).
    graph_path = _graph_path_for_store(args.graph, store_dir)
    try:
        report = build_report(
            store_dir, graph_path if graph_path.exists() else None, window_days=args.window
        )
    except sqlite3.Error as e:
        print(f"cannot read the store index ({index}): {e}", file=sys.stderr)
        return 2
    except UnreadableRecordError as e:
        # Named by table and record id: the message is all the person has to go on.
        print(f"cannot read the store index ({index}): {e}", file=sys.stderr)
        return 2

    # Both renderers are newline-terminated; a `print` here would double it.
    sys.stdout.write(render_json(report) if args.json else render_text(report))
    return 0


def export_okf_main(argv: list[str] | None = None) -> int:
    """``sidegraph-export-okf`` — project the store into an OKF v0.1 bundle (one-way).

    Writes ``--out`` (default ``okf-bundle/``) as an exact snapshot: every Decision and
    Fact (full append-only history, superseded included), domains collapsed one-per-slug,
    and the entities decisions anchor to — OKF concept files with YAML frontmatter and
    bundle-absolute cross-links. Deterministic: same store ⇒ byte-identical bundle. The
    out dir is only ever cleared when empty or marked ``generator: sidegraph`` in its
    root ``index.md``; anything else is refused. Exit 0 on success, 2 on an operational
    error (uninitialized store — never auto-created; refused or unwritable out dir).

    # see design/superpowers/specs/2026-07-23-okf-export-design.md
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-export-okf",
        description="Export the decision store as an OKF v0.1 markdown bundle (one-way).",
    )
    parser.add_argument("--db", default=None, help=_DB_HELP)
    parser.add_argument(
        "--out", default="okf-bundle", help="bundle output directory (default: okf-bundle)"
    )
    args = parser.parse_args(argv)
    args.db = resolve_store_path(args.db, warn_on_create=False)

    if not _looks_already_initialized(Path(args.db)):
        print(f"no store at {args.db} — run `sidegraph-init` first", file=sys.stderr)
        return 2

    bundle = build_bundle(Store(args.db))
    out_dir = Path(args.out)
    try:
        write_bundle(bundle, out_dir)
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    def _count(section: str) -> int:
        return sum(1 for p in bundle if p.startswith(f"{section}/") and p != f"{section}/index.md")

    print(
        f"wrote {out_dir}/ ({_count('decisions')} decisions, {_count('facts')} facts, "
        f"{_count('domains')} domains, {_count('entities')} entities)"
    )
    return 0


def prepare_commit_msg_main(argv: list[str] | None = None) -> int:
    """``sidegraph-prepare-commit-msg`` — git's ``prepare-commit-msg`` hook (design/
    superpowers/specs/2026-08-07-git-bindings-design.md §1). Comments candidate
    ``Sidegraph-Decision:`` trailers into the commit message template for the human (or
    agent) to uncomment — never auto-appended (design non-goal: permanent wrong
    attribution in an immutable trailer is the failure mode this gates against).

    Argument contract (review M6, measured): git invokes a prepare-commit-msg hook as
    ``<message-file> [<source> [<sha1>]]`` — a plain ``git commit`` passes exactly ONE
    arg (source absent). This hook acts only when ``source`` is absent or
    ``"template"``; every other source (``message``/``merge``/``squash``/``commit`` —
    amend) leaves the message file untouched, and any malformed invocation (no args at
    all) is also a no-op.

    Never blocks and never stalls (§0): every internal error, and the mechanism's own
    2s wall-clock budget (:data:`sidegraph.gitio.HOOK_WALL_CLOCK_BUDGET_SECONDS`), degrade
    to writing nothing — this function ALWAYS returns 0. A ``git commit`` must never fail
    or hang because this hook did.
    """
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv:
        return 0
    message_file = argv[0]
    source = argv[1] if len(argv) > 1 else None
    if source not in (None, "", "template"):
        return 0
    try:
        cwd = Path.cwd()
        store_dir = Path(resolve_store_path(None, warn_on_create=False))
        gitio.apply_prepare_commit_msg(message_file, cwd, store_dir)
    except Exception:
        # Never blocks and never stalls (§0/§1 step 3) — an internal error here must
        # never fail (or even warn during) the surrounding `git commit`.
        pass
    return 0


def _format_blame_record(rec: dict) -> str:
    if not rec["resolved"]:
        return f"{rec['id']} (unresolved)"
    tag = f"superseded by {rec['superseded_by']}" if rec["superseded_by"] else rec["kind"]
    return f"{rec['id']} ({tag}): {rec['label']}"


def blame_main(argv: list[str] | None = None) -> int:
    """``sidegraph-blame`` — derived line-level "why" (design §2). ``git blame`` joined
    to the decisions/facts each hunk's commit carries, via TWO paths (deduped): commit
    trailers (``Sidegraph-Decision:``) and ``provenance.commit`` (post-П0, decisions AND
    facts). A record resolved via an old sha that is now superseded prints its
    ``superseded_by`` pointer; an unresolved ULID is listed as unresolved, never guessed.

    Read-only over records (§0): opens the store's index read-only (never a writable
    ``Store()``); a missing/locked index degrades every hunk to unresolved records
    rather than failing the command — blame without a store is still useful blame.

    Output capped at 50 hunk rows / 6000 chars, whichever comes first (review M8); the
    last line states the cap and what was omitted — never a silent truncation.

    Exit codes: 0 on success (JSON or table printed), 1 on an operational git failure
    (not a git repo, unknown file/range, git not installed — G9's CLI error surface;
    unlike the hook, an explicit user command reports this instead of degrading).
    """
    parser = argparse.ArgumentParser(
        prog="sidegraph-blame",
        description="git blame a file, joined to the decisions/facts each hunk's commit "
        "carries (commit trailers + provenance.commit).",
    )
    parser.add_argument("file", help="path to blame, relative to the current directory")
    parser.add_argument("--range", default=None, metavar="A,B", help="line range, e.g. 10,42")
    parser.add_argument("--db", default=None, help=_DB_HELP)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    line_range = None
    if args.range:
        parts = args.range.split(",")
        if len(parts) != 2 or not all(p.strip().lstrip("-").isdigit() for p in parts):
            print(f"sidegraph-blame: invalid --range {args.range!r}, expected A,B", file=sys.stderr)
            return 1
        line_range = (int(parts[0]), int(parts[1]))

    cwd = Path.cwd()
    db_path = Path(resolve_store_path(args.db, warn_on_create=False))
    hunks = gitio.blame_report(args.file, cwd, db_path, line_range)
    if hunks is None:
        print(
            f"sidegraph-blame: git blame failed for {args.file!r} (not a git repo, unknown "
            "file/range, or git not installed)",
            file=sys.stderr,
        )
        return 1

    rows: list[dict] = [
        {
            "start": h.start,
            "end": h.end,
            "sha": h.sha,
            "date": h.date,
            "records": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "label": r.label,
                    "resolved": r.resolved,
                    "superseded_by": r.superseded_by,
                }
                for r in h.records
            ],
        }
        for h in hunks
    ]

    # Output cap (review M8): 50 hunk rows / 6000 chars, whichever first — applied
    # identically to both the human table and --json below, so neither surface silently
    # exceeds it.
    capped: list[dict] = []
    total_chars = 0
    omitted = 0
    for row in rows:
        row_chars = len(json.dumps(row))
        if len(capped) >= gitio.BLAME_ROW_CAP or total_chars + row_chars > gitio.BLAME_CHAR_CAP:
            omitted = len(rows) - len(capped)
            break
        capped.append(row)
        total_chars += row_chars

    if args.json:
        cap_info = {"rows": gitio.BLAME_ROW_CAP, "chars": gitio.BLAME_CHAR_CAP} if omitted else None
        print(
            json.dumps(
                {"file": args.file, "hunks": capped, "omitted": omitted, "cap": cap_info},
                indent=2,
            )
        )
    else:
        print(args.file)
        for row in capped:
            records_str = (
                "; ".join(_format_blame_record(r) for r in row["records"])
                if row["records"]
                else "(no decision/fact recorded)"
            )
            line_str = (
                f"{row['start']}" if row["start"] == row["end"] else f"{row['start']}-{row['end']}"
            )
            print(f"  {line_str}\t{row['sha'][:12]}\t{row['date'] or '?'}\t{records_str}")
        if omitted:
            print(
                f"... {omitted} more hunk row(s) omitted "
                f"(cap: {gitio.BLAME_ROW_CAP} rows / {gitio.BLAME_CHAR_CAP} chars)"
            )
    return 0
