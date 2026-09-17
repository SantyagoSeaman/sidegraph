"""Auto-ratification policy — Task 1: pure parser, shell threading, frozen goldens.

Spec: design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md (D1, T1,
T9). This task wires the ``SIDEGRAPH_RATIFY_POLICY`` keyword through every entry point
with NO auto-ratification behavior attached yet (Tasks 2-5 add the eligibility gates and
the post-write transition) -- every test here proves plumbing, not policy effect.

This module is also the SHARED SCAFFOLDING FILE for the whole plan (see
``.superpowers/sdd/2026-09-11-auto-ratification-policy/test-inventory.md``): the plan
puts every spec ledger row's tests here (T1-T7, T9-T19; T8 gets its own
``tests/test_doctor_auto_share.py``), Task 1 establishes it and Tasks 2-7 APPEND --
never re-declaring a builder that already exists here. The names below this module's own
Step 1/4 tests (``_no_ambient_initiative``, ``store``, ``GOLDEN_GRAPH``, ``FEATURE_GRAPH``/
``_feature_graph``, ``_eligible_decision_draft``/``_eligible_fact_draft``/
``_eligible_domain_draft``) are that scaffolding -- unused by Task 1's own tests (T1's row
needs none of them; see the test-inventory table), present so Tasks 2-7 do not each
reinvent one, per that inventory's own "no two tasks invent the same builder" rule.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph import capture
from sidegraph.anchoring import resolve_and_bind
from sidegraph.capture import RatifyPolicy, auto_ratify_eligible
from sidegraph.cli import domains_main, import_main
from sidegraph.doc_import import import_docs
from sidegraph.domains import bootstrap_domains
from sidegraph.engine.reader import GraphifyReader
from sidegraph.importer import import_rationales
from sidegraph.schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Fact,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.sync import refresh_domain_communities_now

# Task 5 cross-file fixture aliases (test-inventory.md "no two tasks invent the same
# builder" rule) -- reuse doc_import's own committed test helpers/fixtures rather than a
# parallel set; `tests.test_cli_import`/`tests.test_domains`/`tests.test_cli_domains`
# are imported locally, per-test, following this file's own established pattern.
from tests.test_doc_import import IDENTIFIER_GRAPH as _DOC_IDENTIFIER_GRAPH
from tests.test_doc_import import _adr as _doc_adr
from tests.test_doc_import import _adr_with_frontmatter_status as _doc_adr_with_status
from tests.test_doc_import import _reader as _doc_reader
from tests.test_doc_import import _write_md as _doc_write_md

# `tests` is importable as a package (pythonpath = ["."] plus tests/__init__.py) -- a
# cross-test import, same precedent as `tests/test_corpus_leak_gate.py`'s import from
# `tests.test_release_allowlist`. `_proposed()` is Task 3's brief-mandated helper (not
# reinvented here): a minimal PROPOSED `Decision` via the real `store` fixture shape.
from tests.test_store_ratification import _proposed

# `tests/fixtures/mini_graph.json` -- committed, read-only, used VERBATIM by the T9/T19
# goldens (never extended: test_capture_propose.py/test_capture_facts.py/
# test_capture_neighbors.py assert against its current exact contents). 3 nodes, 2
# communities, all DISTINCT labels (Trader/place_order() in trader/exec.py, community 1;
# helper() in util/misc.py, community 2) -- see FEATURE_GRAPH below for the ambiguous
# case this graph structurally cannot produce.
GOLDEN_GRAPH = Path(__file__).parent / "fixtures" / "mini_graph.json"
GOLDENS = Path(__file__).parent / "fixtures" / "auto_policy" / "parity_goldens.json"


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    """Tests must not depend on the ambient git branch (established repo pattern --
    ``tests/test_capture_propose.py:15``, ``tests/test_capture_auto_accept.py:17``).

    Without it, ``capture._derive_initiative()`` picks up whatever branch this checkout
    happens to be on and silently adds a stray Tier-0 binding, which anchor/binding-count
    assertions in Tasks 2-7's eligibility tests don't account for. Autouse: every test in
    this shared scaffolding file gets it for free, the same way every test in
    ``test_capture_propose.py`` already does.
    """
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


@pytest.fixture
def store(tmp_path) -> Store:
    """Shared per-test store (test-inventory.md scaffolding, spec rows T2-T19 generally) --
    this repo's established fixture name/shape (``tests/test_store.py:22``,
    ``tests/test_store_facts.py:24``, six more): request it as a normal test parameter
    (``def test_foo(store): ...``), never call it directly. The context-manager form
    closes the sqlite connection on teardown, same as every other module's copy.
    """
    with Store(tmp_path / "t.db") as s:
        yield s


# Purpose-built graph for every eligibility/wiring test (Tasks 2-6, spec rows T2-T6/
# T10-T18): GOLDEN_GRAPH's three labels are all distinct, so an AMBIGUOUS anchor (a name
# matching more than one node, ``GraphifyReader.resolve`` -> ``status="ambiguous"``) is
# structurally unreachable over it (spec rows T4/T13 need exactly that state). This graph
# is GOLDEN_GRAPH's same three nodes plus a duplicated pair -- two nodes both labelled
# ``retry()`` in ``trader/retry.py``, community 1 -- so `retry()` is ambiguous while
# `Trader`/`place_order()`/`helper()` stay resolvable exactly as in GOLDEN_GRAPH. Modeled
# on the repo's existing two-same-label precedent, ``tests/test_anchoring.py``'s
# ``test_ambiguous_real_reader_degrades_to_community``.
FEATURE_GRAPH: dict = {
    "directed": True,
    "multigraph": False,
    "built_at_commit": "feature-graph",
    "nodes": [
        {
            "id": "m_cls",
            "label": "Trader",
            "norm_label": "trader",
            "file_type": "code",
            "source_file": "trader/exec.py",
            "source_location": "L10",
            "community": 1,
            "_origin": "ast",
        },
        {
            "id": "m_fn",
            "label": "place_order()",
            "norm_label": "place_order()",
            "file_type": "code",
            "source_file": "trader/exec.py",
            "source_location": "L20",
            "community": 1,
            "_origin": "ast",
        },
        {
            "id": "o_fn",
            "label": "helper()",
            "norm_label": "helper()",
            "file_type": "code",
            "source_file": "util/misc.py",
            "source_location": "L3",
            "community": 2,
            "_origin": "ast",
        },
        {
            "id": "r_fn_a",
            "label": "retry()",
            "norm_label": "retry()",
            "file_type": "code",
            "source_file": "trader/retry.py",
            "source_location": "L5",
            "community": 1,
            "_origin": "ast",
        },
        {
            "id": "r_fn_b",
            "label": "retry()",
            "norm_label": "retry()",
            "file_type": "code",
            "source_file": "trader/retry.py",
            "source_location": "L40",
            "community": 1,
            "_origin": "ast",
        },
    ],
    "links": [
        {"source": "m_cls", "target": "m_fn", "relation": "contains", "confidence": "EXTRACTED"},
        {"source": "m_fn", "target": "o_fn", "relation": "calls", "confidence": "EXTRACTED"},
    ],
}


def _feature_graph(tmp_path: Path) -> GraphifyReader:
    """Write ``FEATURE_GRAPH`` fresh into ``tmp_path`` (never the committed
    ``GOLDEN_GRAPH`` -- see that constant's docstring) and return a ready
    ``GraphifyReader`` over it. Consumed by spec rows T2-T6/T10-T18 (Tasks 2/4's
    eligibility and cascade/supersession/lint tests). A caller that needs the raw path
    instead of a reader has it on the returned object: ``GraphifyReader.path`` (see
    ``bootstrap/apply.py:110``'s ``GraphifyReader(reader.path)`` for the existing
    precedent of reading it back off a reader)."""
    path = tmp_path / "feature_graph.json"
    path.write_text(json.dumps(FEATURE_GRAPH))
    return GraphifyReader(path)


def _eligible_decision_draft(**over) -> dict:
    """The known-good ``DraftDecision`` shape (test-inventory.md scaffolding, spec rows
    T2/T3/T4/T11/T12/T17): a ``gotcha`` with a live, unambiguous anchor (``Trader`` @
    ``trader/exec.py``, resolves cleanly on both ``GOLDEN_GRAPH`` and ``FEATURE_GRAPH``)
    and no ``supersedes``. Every ineligibility test in Tasks 2-6 should override exactly
    ONE field of this shape, so the test proves the gate it names and nothing else (a
    test that builds its own unrelated draft proves the record stayed proposed without
    proving *which* gate stopped it)."""
    base: dict = {
        "title": "eligible gotcha draft",
        "kind": "gotcha",
        "context": "a reproducible mistake, reliably triggered",
        "choice": "the fix that was applied",
        "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
    }
    base.update(over)
    return base


def _eligible_fact_draft(**over) -> dict:
    """The known-good standalone ``DraftFact`` shape (test-inventory.md scaffolding,
    spec rows T2/T4/T5/T11): statement + source + its own live, unambiguous anchor --
    same one-field-override discipline as ``_eligible_decision_draft``."""
    base: dict = {
        "statement": "a hard-won, non-derivable fact",
        "source": "benchmark run",
        "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
    }
    base.update(over)
    return base


def _eligible_domain_draft(**over) -> dict:
    """The known-good ``DraftDomain`` shape (test-inventory.md scaffolding, spec rows
    T6/T13): slug/title/summary + a seed anchor that resolves + no ``path_prefixes`` (so
    ``_lint_domain_path_prefixes`` has nothing to warn about) -- same one-field-override
    discipline as ``_eligible_decision_draft``."""
    base: dict = {
        "slug": "trading",
        "title": "Trading",
        "summary": "Order execution path.",
        "seed_anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
    }
    base.update(over)
    return base


def test_feature_graph_offers_resolvable_and_ambiguous_anchors(tmp_path):
    """Guards the shared scaffolding itself: a break in ``FEATURE_GRAPH`` would otherwise
    surface as a confusing failure three tasks away, in whichever Task 2/4 eligibility test
    happens to hit it first, rather than here at its source. ``Trader`` (also present on
    ``GOLDEN_GRAPH``) must stay resolvable; ``retry()`` -- the whole reason this graph
    exists -- must stay genuinely ambiguous (2 candidates), never orphaned or resolved."""
    reader = _feature_graph(tmp_path)
    assert (
        reader.resolve(Descriptor(name="Trader", file_path="trader/exec.py")).status == "resolved"
    )
    result = reader.resolve(Descriptor(name="retry()"))
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2


# ── Step 1: parser ───────────────────────────────────────────────────────────────────


def test_unknown_policy_fails_safe_to_manual():
    assert capture.parse_ratify_policy("yes-please") == capture.RatifyPolicy.MANUAL


def test_none_is_manual():
    assert capture.parse_ratify_policy(None) == capture.RatifyPolicy.MANUAL


def test_empty_string_is_manual():
    """The `_proposal_window_days` fail-safe precedent applies to blank, not just
    unrecognized, values -- a `SIDEGRAPH_RATIFY_POLICY=""` env line must not open the
    gate either."""
    assert capture.parse_ratify_policy("") == capture.RatifyPolicy.MANUAL


def test_known_values_parse_to_the_matching_member():
    assert capture.parse_ratify_policy("manual") == RatifyPolicy.MANUAL
    assert capture.parse_ratify_policy("auto-low-risk") == RatifyPolicy.AUTO_LOW_RISK
    assert capture.parse_ratify_policy("auto-all") == RatifyPolicy.AUTO_ALL


def test_padded_value_still_parses_instead_of_silently_downgrading():
    """A trailing/leading space off a `.env` line (`SIDEGRAPH_RATIFY_POLICY="auto-all "`)
    must not silently fall back to `manual` -- an autonomous deployment has nobody
    watching to notice the quiet downgrade (review round 2, promoted Minor 5)."""
    assert capture.parse_ratify_policy("auto-all ") == RatifyPolicy.AUTO_ALL
    assert capture.parse_ratify_policy(" auto-low-risk\n") == RatifyPolicy.AUTO_LOW_RISK
    assert capture.parse_ratify_policy("   ") == RatifyPolicy.MANUAL  # whitespace-only


# ── Step 1: every entry point exposes the defaulted keyword ─────────────────────────


def test_entry_points_default_manual_and_take_kwarg():
    """All six C-9 core entry points, plus the two MCP testable cores that own the
    combined/domain requests, expose ``ratify_policy: RatifyPolicy = MANUAL``."""
    from sidegraph import doc_import, domains, importer, server

    for fn in (
        capture.propose,
        capture.propose_facts,
        capture.propose_domains,
        importer.import_rationales,
        doc_import.import_docs,
        domains.bootstrap_domains,
        server._propose_decisions_impl,
        server._propose_domains_impl,
    ):
        assert (
            inspect.signature(fn).parameters["ratify_policy"].default == capture.RatifyPolicy.MANUAL
        ), fn.__qualname__


# ── Step 1: one-parse-per-MCP-request identity tests (spec C-9/T1) ──────────────────


def _spy_env_reads(monkeypatch, key: str) -> list[str]:
    """Count reads of ``key`` through ``os.environ.get`` itself -- the actual invariant
    behind "the env is read once per invocation" (review round 3, Important 2 / Ruling
    O). A parse-route spy (``capture.parse_ratify_policy`` / ``server.parse_ratify_policy``)
    is bypassable: constructing the enum directly from a second
    ``RatifyPolicy(os.environ.get("SIDEGRAPH_RATIFY_POLICY", "manual"))`` read inside
    ``_propose_decisions_impl`` skips the parser -- and the resolver spy that wraps it --
    entirely; reproduced independently, that mutation left all 29 tests in this module
    green with only the parse-route spies in place. Patching ``os.environ.get`` catches
    every route that reaches it, regardless of which function calls it. Filters to
    ``key`` so unrelated reads elsewhere in the same call (``SIDEGRAPH_GRAPH``,
    ``SIDEGRAPH_DB``, ...) don't pollute the count.
    """
    calls: list[str] = []
    real_get = os.environ.get

    def counting_get(k, default=None):
        if k == key:
            calls.append(k)
        return real_get(k, default)

    monkeypatch.setattr(os.environ, "get", counting_get)
    return calls


def test_combined_mcp_call_samples_policy_once_and_shares_the_object(tmp_path, monkeypatch):
    """One MCP ``propose_decisions`` request carrying BOTH decision drafts and a
    top-level standalone fact resolves ``SIDEGRAPH_RATIFY_POLICY`` exactly once
    (``server._ratify_policy``) and threads the IDENTICAL parsed object down through
    ``_propose_decisions_impl`` into both ``capture.propose`` and
    ``capture.propose_facts`` -- a per-core-call env read would sample twice in this one
    request; this red-flags that regression at the core-call boundary, not just the
    resolver's own call count.

    Spies BOTH ``server._ratify_policy`` (the resolver) and ``server.parse_ratify_policy``
    (the pure parser it wraps) -- a resolver-only spy is route-BOUND: a regression that
    re-resolves through ``_ratify_policy()`` a second time is caught, but a literal
    per-core-call env read placed directly inside ``_propose_decisions_impl``
    (``ratify_policy=parse_ratify_policy(os.environ.get(...))``, bypassing
    ``_ratify_policy()`` entirely) calls the parser twice while the resolver spy still
    reads 1 -- review round 2, Important 1, verified: this exact mutation survived all
    nine tests with only the resolver spy in place. Counting parser calls closes that
    gap regardless of which route reaches it.

    Also spies ``os.environ.get`` itself (``_spy_env_reads``) for the actual invariant:
    even the parser-call count above is dodgeable by a mutation that constructs
    ``RatifyPolicy(os.environ.get(...))`` directly, bypassing both ``_ratify_policy()``
    AND ``parse_ratify_policy`` (review round 3, Important 2 / Ruling O, reproduced
    independently -- see ``_spy_env_reads``'s docstring). The parse-count and
    object-identity assertions below are kept as-is; the env-read count is additive.
    """
    from sidegraph import server

    monkeypatch.setattr(server, "_store", Store(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    # Never resolve anchors against whatever graph this machine happens to have (review
    # round 2, promoted Minor 3) -- this file is scaffolding Tasks 2-6 append to.
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(_feature_graph(tmp_path).path))

    env_reads = _spy_env_reads(monkeypatch, "SIDEGRAPH_RATIFY_POLICY")

    resolver_calls: list[RatifyPolicy] = []
    real_ratify_policy = server._ratify_policy

    def spy_ratify_policy() -> RatifyPolicy:
        resolved = real_ratify_policy()
        resolver_calls.append(resolved)
        return resolved

    monkeypatch.setattr(server, "_ratify_policy", spy_ratify_policy)

    parse_calls: list[str | None] = []
    real_parse = server.parse_ratify_policy

    def spy_parse(raw: str | None) -> RatifyPolicy:
        parse_calls.append(raw)
        return real_parse(raw)

    monkeypatch.setattr(server, "parse_ratify_policy", spy_parse)

    propose_policies: list[RatifyPolicy] = []
    real_propose = server.propose

    def spy_propose(*args, **kwargs):
        propose_policies.append(kwargs["ratify_policy"])
        return real_propose(*args, **kwargs)

    monkeypatch.setattr(server, "propose", spy_propose)

    propose_facts_policies: list[RatifyPolicy] = []
    real_propose_facts = server.propose_facts

    def spy_propose_facts(*args, **kwargs):
        propose_facts_policies.append(kwargs["ratify_policy"])
        return real_propose_facts(*args, **kwargs)

    monkeypatch.setattr(server, "propose_facts", spy_propose_facts)

    draft = {
        "title": "lock around order placement",
        "kind": "gotcha",
        "context": "races seen in production",
        "choice": "lock around order placement",
        "anchors": [],
    }
    fact = {
        "statement": "place_order() retries at most 3 times.",
        "source": "benchmark run",
        "anchors": [{"name": "place_order"}],
    }

    results = server.propose_decisions(drafts=[draft], facts=[fact])

    assert len(results) == 2  # 1 decision result + 1 standalone-fact result, unchanged
    assert len(env_reads) == 1  # the env was READ once, whatever route reaches it
    assert len(resolver_calls) == 1  # sampled ONCE for the whole request
    assert resolver_calls[0] == RatifyPolicy.AUTO_ALL
    assert len(parse_calls) == 1  # the env was PARSED once, by ANY route (route-independent)
    assert propose_policies == [RatifyPolicy.AUTO_ALL]
    assert propose_facts_policies == [RatifyPolicy.AUTO_ALL]
    # RatifyPolicy is a StrEnum, so this is equivalent to the `==` checks above (its
    # members are singletons) -- kept as a sanity check on the threading, not as a
    # stronger guard than equality; `len(parse_calls) == 1` above is what actually proves
    # one parse per request.
    assert propose_policies[0] is propose_facts_policies[0] is resolver_calls[0]


def test_domain_mcp_call_samples_policy_once_and_threads_it(tmp_path, monkeypatch):
    """One MCP ``propose_domains`` request resolves the policy exactly once and the
    resolved object reaches ``capture.propose_domains`` (aliased ``_propose_domain_drafts``
    in ``server.py``) unchanged, via ``_propose_domains_impl``.

    Spies ``server.parse_ratify_policy`` alongside the resolver -- see the sibling
    decisions/facts test's docstring for why a resolver-only spy is route-bound and does
    not catch a per-core-call env read (review round 2, Important 1). Also spies
    ``os.environ.get`` itself (``_spy_env_reads``) for the actual invariant a parse-route
    spy cannot catch (review round 3, Important 2 / Ruling O).
    """
    from sidegraph import server

    monkeypatch.setattr(server, "_store", Store(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-low-risk")
    # Never resolve anchors against whatever graph this machine happens to have (review
    # round 2, promoted Minor 3).
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(_feature_graph(tmp_path).path))

    env_reads = _spy_env_reads(monkeypatch, "SIDEGRAPH_RATIFY_POLICY")

    resolver_calls: list[RatifyPolicy] = []
    real_ratify_policy = server._ratify_policy

    def spy_ratify_policy() -> RatifyPolicy:
        resolved = real_ratify_policy()
        resolver_calls.append(resolved)
        return resolved

    monkeypatch.setattr(server, "_ratify_policy", spy_ratify_policy)

    parse_calls: list[str | None] = []
    real_parse = server.parse_ratify_policy

    def spy_parse(raw: str | None) -> RatifyPolicy:
        parse_calls.append(raw)
        return real_parse(raw)

    monkeypatch.setattr(server, "parse_ratify_policy", spy_parse)

    domain_policies: list[RatifyPolicy] = []
    real_propose_domain_drafts = server._propose_domain_drafts

    def spy_propose_domain_drafts(*args, **kwargs):
        domain_policies.append(kwargs["ratify_policy"])
        return real_propose_domain_drafts(*args, **kwargs)

    monkeypatch.setattr(server, "_propose_domain_drafts", spy_propose_domain_drafts)

    draft = {"slug": "trading", "title": "Trading", "summary": "Order execution path."}
    results = server.propose_domains(drafts=[draft])

    assert len(results) == 1
    assert len(env_reads) == 1  # the env was READ once, whatever route reaches it
    assert len(resolver_calls) == 1
    assert resolver_calls[0] == RatifyPolicy.AUTO_LOW_RISK
    assert len(parse_calls) == 1  # the env was PARSED once, by ANY route
    assert domain_policies == [RatifyPolicy.AUTO_LOW_RISK]
    # StrEnum singleton, same caveat as the sibling test: a sanity check, not a stronger
    # guard than the `==`/parse-count assertions above.
    assert domain_policies[0] is resolver_calls[0]


# ── Step 1 (round 3 fix): the three CLI batch entry points sample once too ──────────
#
# Review round 3, Important 2 / Ruling O: no sampling test existed at all for the three
# CLI batch commands, which read `SIDEGRAPH_RATIFY_POLICY` directly at their own call
# site (`cli.py:702` doc-import, `:937` rationale-import, `:1042` domain-bootstrap)
# rather than through `server._ratify_policy()`. Each spies `os.environ.get` itself via
# `_spy_env_reads` -- the CLI has no parse-route resolver to spy on in the first place,
# which is exactly why the underlying invariant has to be pinned at the env read. Each
# test passes `--graph` explicitly (the repo's own CLI-test convention), so none of them
# depend on `SIDEGRAPH_GRAPH` or any other ambient machine state.


def test_cli_doc_import_samples_ratify_policy_once(tmp_path, monkeypatch):
    """``sidegraph-import --docs`` (``_import_docs_mode``, ``cli.py:702``) reads
    ``SIDEGRAPH_RATIFY_POLICY`` exactly once per invocation, resolved immediately before
    the batch ``import_docs`` call -- not once per document. Two docs in one invocation
    is what actually exercises the "per batch, not per document" half; a single-doc
    invocation would pass even under a per-document regression."""
    from tests.test_cli_import import _DOC_MENTION_GRAPH, _adr_md, _write_md

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_DOC_MENTION_GRAPH))
    db = tmp_path / "t.db"
    _write_md(tmp_path, "docs/a.md", _adr_md("Doc A"))
    _write_md(tmp_path, "docs/b.md", _adr_md("Doc B"))

    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    env_reads = _spy_env_reads(monkeypatch, "SIDEGRAPH_RATIFY_POLICY")

    rc = import_main(
        ["--db", str(db), "--graph", str(graph), "--docs", str(tmp_path / "docs"), "--any-doc"]
    )
    assert rc == 0
    assert len(env_reads) == 1


def test_cli_rationale_import_samples_ratify_policy_once(tmp_path, monkeypatch):
    """``sidegraph-import`` rationale mode (``import_main``, ``cli.py:937``) reads
    ``SIDEGRAPH_RATIFY_POLICY`` exactly once per invocation."""
    from tests.test_cli_import import GRAPH as _rationale_graph

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_rationale_graph))
    db = tmp_path / "t.db"

    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-low-risk")
    env_reads = _spy_env_reads(monkeypatch, "SIDEGRAPH_RATIFY_POLICY")

    assert import_main(["--db", str(db), "--graph", str(graph)]) == 0
    assert len(env_reads) == 1


def test_cli_domain_bootstrap_samples_ratify_policy_once(tmp_path, monkeypatch):
    """``sidegraph-domains bootstrap`` (``_domains_bootstrap``, ``cli.py:1042``) reads
    ``SIDEGRAPH_RATIFY_POLICY`` exactly once per invocation -- proposing 2 domains from
    2 communities is what exercises the "per batch, not per candidate" half."""
    from tests.test_cli_domains import GRAPH as _bootstrap_graph
    from tests.test_cli_domains import LABELS as _bootstrap_labels

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    db = tmp_path / "t.db"

    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "manual")
    env_reads = _spy_env_reads(monkeypatch, "SIDEGRAPH_RATIFY_POLICY")

    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    assert len(env_reads) == 1


# ── Step 4: T9 golden fixture builder + freeze ───────────────────────────────────────
#
# The store is a pure function of `now`: every `valid_from` is RELATIVE to it (accepted
# rows `now - 40d`, proposed rows `now - 3d`, well inside the 30-day proposal window with
# margin) rather than the caller's real wall-clock time, because Task 7 rebuilds this same
# fixture under a clock shifted +200 days and requires byte-identical rendered output.
# Decisions/facts are therefore constructed directly (`Decision(...)`/`Fact(...)` +
# `store.add_decision`/`add_fact`, the same shape `server._add_decision_impl` -- the
# human-asked add path -- already uses) rather than through `capture.propose*`, which
# hardcodes real `datetime.now(UTC)` for `valid_from` and would make the fixture a
# function of wall-clock time instead of the `now` argument. `Domain` carries no
# `valid_from` field at all (schema.py), so domains are built through the REAL
# propose/ratify/activate path (`capture.propose_domains` -> `store.ratify_domains` ->
# `sync.refresh_domain_communities_now`, mirroring `cli.py`'s own ratify flow) with no
# clock concern.


def _decision(
    store: Store,
    reader: GraphifyReader,
    *,
    record_id: str,
    title: str,
    kind: DecisionKind,
    status: DecisionStatus,
    valid_from: datetime,
    anchor_name: str,
    anchor_path: str,
) -> Decision:
    d = Decision(
        id=record_id,
        title=title,
        kind=kind,
        status=status,
        context=f"why: {title}",
        choice=f"decided: {title}",
        valid_from=valid_from,
        provenance=Provenance(source="agent", author="alex"),
    )
    if status == DecisionStatus.ACCEPTED:
        d.ratified_by = "alex"
        d.ratified_at = valid_from
    store.add_decision(d)
    resolve_and_bind(d.id, Descriptor(name=anchor_name, file_path=anchor_path), reader, store)
    return d


# Fixed (never regenerated) ids for the golden fixture's decisions/fact -- `Decision.id`/
# `Fact.id` default to a fresh random ULID (`schema._new_id`), which would make every
# `_golden_store` rebuild mint different ids and break BYTE-IDENTICAL comparison the
# moment a render embeds one (`get_task_context`'s "(id: ...)" suffix on each decision
# line). Real ULID-shaped strings (picked once, hardcoded) rather than readable slugs, so
# nothing downstream that happens to assume the shape is surprised.
_ACCEPTED_GOTCHA_ID = "01M2B0F19RPYSVTTWG9D6XSHZW"
_ACCEPTED_ADR_ID = "01M2B0F19RPYSVTTWG9D6XSHZX"
_ACCEPTED_LESSON_ID = "01M2B0F19RPYSVTTWG9D6XSHZY"
_PROPOSED_GOTCHA_ID = "01M2B0F19RPYSVTTWG9D6XSHZZ"
_PROPOSED_LESSON_ID = "01M2B0F19RPYSVTTWG9D6XSJ00"
_PROPOSED_FACT_ID = "01M2B0F19RPYSVTTWG9D6XSJ01"


def _golden_store(tmp_path: Path, now: datetime, *, proposed_from: datetime | None = None) -> Store:
    """Build the T9 parity-golden fixture store (spec ledger T9): 2 accepted domains (one
    per ``tests/fixtures/mini_graph.json`` community), 3 accepted decisions (incl. one
    gotcha), 2 proposed decisions, and 1 proposed standalone fact -- anchored over that
    same committed fixture graph. See the module-level comment above for why every
    ``valid_from`` is computed relative to ``now`` rather than real wall-clock time.

    ``proposed_from`` (Task 7, keyword-only, defaults to ``now - 3 days`` exactly as
    before) lets a caller pin the two proposed decisions' and the proposed fact's
    ``valid_from`` to an ABSOLUTE instant instead of one relative to ``now`` -- the only
    way to build a fixture where the render clock and the record age have drifted apart
    on purpose (the T9 calendar bite test). Every existing caller passes nothing here and
    is unaffected."""
    store = Store(tmp_path / "golden.db")
    reader = GraphifyReader(GOLDEN_GRAPH)

    accepted_from = now - timedelta(days=40)
    if proposed_from is None:
        proposed_from = now - timedelta(days=3)

    # -- 2 accepted domains, one per mini_graph.json community (1: Trader/place_order(),
    # 2: helper()) -- real propose/ratify/activate path, no valid_from concern (Domain has
    # none).
    domain_results = capture.propose_domains(
        [
            {
                "slug": "trading",
                "title": "Trading",
                "summary": "Order execution path.",
                "seed_anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
            },
            {
                "slug": "utilities",
                "title": "Utilities",
                "summary": "Shared helper functions.",
                "seed_anchors": [{"name": "helper()", "file_path": "util/misc.py"}],
            },
        ],
        store,
        reader,
        author="alex",
    )
    assert all(r.status == "proposed" for r in domain_results), domain_results
    domain_ids = [r.domain_id for r in domain_results if r.domain_id]
    outcomes = store.ratify_domains(accept=domain_ids)
    assert all(v == "accepted" for v in outcomes.values()), outcomes
    for domain_id in domain_ids:
        domain = store.get_domain(domain_id)
        assert domain is not None
        refresh_domain_communities_now(domain, store, reader)
    from sidegraph.retrieval import TOC_CACHE_KEY, build_toc

    store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))

    # -- 3 accepted decisions (incl. one gotcha), `valid_from = now - 40d` -------------
    _decision(
        store,
        reader,
        record_id=_ACCEPTED_GOTCHA_ID,
        title="Lock around order placement",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        valid_from=accepted_from,
        anchor_name="place_order()",
        anchor_path="trader/exec.py",
    )
    _decision(
        store,
        reader,
        record_id=_ACCEPTED_ADR_ID,
        title="Use ULID for entity ids",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        valid_from=accepted_from,
        anchor_name="Trader",
        anchor_path="trader/exec.py",
    )
    _decision(
        store,
        reader,
        record_id=_ACCEPTED_LESSON_ID,
        title="helper() stays side-effect free",
        kind=DecisionKind.LESSON,
        status=DecisionStatus.ACCEPTED,
        valid_from=accepted_from,
        anchor_name="helper()",
        anchor_path="util/misc.py",
    )

    # -- 2 proposed decisions, `valid_from = now - 3d` ---------------------------------
    _decision(
        store,
        reader,
        record_id=_PROPOSED_GOTCHA_ID,
        title="place_order() may double-submit on retry",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.PROPOSED,
        valid_from=proposed_from,
        anchor_name="place_order()",
        anchor_path="trader/exec.py",
    )
    _decision(
        store,
        reader,
        record_id=_PROPOSED_LESSON_ID,
        title="helper() may need memoization",
        kind=DecisionKind.LESSON,
        status=DecisionStatus.PROPOSED,
        valid_from=proposed_from,
        anchor_name="helper()",
        anchor_path="util/misc.py",
    )

    # -- 1 proposed standalone fact, `valid_from = now - 3d` ---------------------------
    fact = Fact(
        id=_PROPOSED_FACT_ID,
        statement="place_order() retries at most 3 times.",
        source="benchmark run",
        status=DecisionStatus.PROPOSED,
        valid_from=proposed_from,
        provenance=Provenance(source="agent", author="alex"),
    )
    store.add_fact(fact)
    resolve_and_bind(
        fact.id, Descriptor(name="place_order()", file_path="trader/exec.py"), reader, store
    )

    return store


def _frozen_datetime(fixed: datetime) -> type[datetime]:
    """A ``datetime`` subclass whose ``.now()`` always returns ``fixed`` -- swapped in for
    the module-level ``datetime`` NAME in ``hooks``/``retrieval`` (both
    ``from datetime import datetime``, per T9), never for the stdlib class itself.
    Subclassing (not a bare stand-in) keeps every other ``datetime`` call (construction,
    arithmetic, comparisons) working exactly as before -- only ``.now()`` is pinned.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed.astimezone(tz) if tz is not None else fixed

    return _Frozen


def _render_parity_surfaces(
    store: Store,
    reader: GraphifyReader | None,
    graph_path: Path,
    *,
    now: datetime,
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Render the T9 parity surfaces against ``store``/``reader`` on the CURRENT revision:
    "task-context + TOC + nudge + queue counts" (spec ledger T9). Self-contained (saves
    and restores every env var, ``sys.stdin``, and the two clock names it patches) so both
    the one-off golden-freeze step and Task 7's later comparison call it and get the exact
    same text for the exact same store, with no hand-reimplemented rendering to drift out
    of sync with the real code.

    ``now`` is patched into ``hooks.datetime``/``retrieval.datetime`` (module-level NAMES,
    per T9 -- both do ``from datetime import datetime``) for the DURATION of this call,
    exactly like Task 7's shifted-clock run will do. Required, not cosmetic: ``hooks.py``'s
    ratification-queue line computes ``(datetime.now(UTC) - min(stamps)).days`` and
    ``retrieval.py``'s proposal window computes ``datetime.now(UTC) - record.valid_from``
    -- both against REAL wall-clock time otherwise, which would make "oldest N days"
    (and the window's include/exclude boundary) drift upward every day this test suite
    runs, even though ``_golden_store`` built every ``valid_from`` relative to ``now``.
    Freezing the render's own clock to the SAME ``now`` used to build the store is what
    makes the golden stable forever, not just at freeze time.

    ``extra_env`` (Task 7, keyword-only): extra environment variables applied AFTER this
    function's own pops (so a caller can put e.g. ``SIDEGRAPH_RATIFY_POLICY`` back into
    the environment for the duration of the render) -- T9's declared read-path-leak arm
    renders under ``{"SIDEGRAPH_RATIFY_POLICY": "auto-all"}`` and asserts the golden is
    unaffected, since neither ``retrieval.py`` nor ``host/hooks.py`` ever reads that
    variable. Every key in ``extra_env`` is folded into ``env_keys`` BEFORE the save below
    runs, so it is captured pre-call and restored in ``finally`` exactly like the fixed
    keys are -- a key outside the fixed tuple (unlike ``SIDEGRAPH_RATIFY_POLICY`` here,
    which already is one) would otherwise leak into whatever test runs next.

    Four keys:
    - ``"task_context"``: ``server._get_task_context_impl`` seeded with
      ``files=["trader/exec.py"]`` (mistakes-first) -- the "task-context" surface. A
      seedless call renders the generic "No context found." for every store, which would
      guard nothing; this file matches the fixture graph and pulls in decisions/facts
      anchored to both communities via the structural map's peripheral entities.
    - ``"toc"``: ``retrieval.render_toc(retrieval.build_toc(store))`` -- the domain-named
      "TOC" surface, most directly exercising retrieval.py's proposal-window gate
      (T9's second clock-dependent renderer, ``retrieval.py:568-571``).
    - ``"session_start"``: the FULL ``additionalContext`` string the real
      ``host.hooks.session_start`` hook emits for this store/graph -- embeds the
      ratification-queue "nudge" line (T9's first clock-dependent renderer, the
      "oldest N days" clause at ``hooks.py:348``) and the drift line, if any, alongside
      its own TOC render. Captured by actually invoking the hook rather than
      hand-reimplementing its text, so a future edit to that render can't silently
      diverge from what this golden pins.
    - ``"queue_counts"``: ``store.pending_ratification_counts()`` as a plain
      ``[decisions, facts, domains]`` list -- a numeric "queue counts" parity check
      independent of any surrounding prose, so a regression in the counting logic itself
      is caught even if the text wrapping it is later reworded.
    """
    import sidegraph.retrieval as retrieval_module
    from sidegraph import server as server_module
    from sidegraph.host import hooks as hooks_module
    from sidegraph.retrieval import build_toc, render_toc

    env_keys = (
        "SIDEGRAPH_DIR",
        "SIDEGRAPH_DB",
        "SIDEGRAPH_GRAPH",
        "CLAUDE_PROJECT_DIR",
        "SIDEGRAPH_RATIFY_NUDGE",
        "SIDEGRAPH_DRIFT_NUDGE",
        "SIDEGRAPH_UNRATIFIED",
        # The other half of retrieval.py's proposal_surfaces() gate (retrieval.py:553,
        # two lines above SIDEGRAPH_UNRATIFIED) -- review round 2, Important 2. Missing
        # this meant the FREEZE itself was unprotected: had the generating shell carried
        # this variable, the committed golden would have frozen the wrong text
        # permanently (goldens regenerate only by owner decision).
        "SIDEGRAPH_PROPOSAL_WINDOW_DAYS",
        "SIDEGRAPH_RATIFY_POLICY",
    )
    if extra_env:
        # Fold in BEFORE the save, so a key outside the fixed tuple above is captured
        # pre-call and restored below too, never left to leak into a later test.
        env_keys = env_keys + tuple(extra_env)
    saved_env = {k: os.environ.get(k) for k in env_keys}
    saved_stdin = sys.stdin
    frozen = _frozen_datetime(now)
    saved_hooks_datetime = hooks_module.datetime
    saved_retrieval_datetime = retrieval_module.datetime

    try:
        for k in env_keys:
            os.environ.pop(k, None)
        os.environ["SIDEGRAPH_DB"] = str(store.path)
        os.environ["SIDEGRAPH_GRAPH"] = str(graph_path)
        for k, v in (extra_env or {}).items():
            os.environ[k] = v
        sys.stdin = io.StringIO("")
        hooks_module.datetime = frozen
        retrieval_module.datetime = frozen

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks_module.session_start()
        session_start_out = json.loads(buf.getvalue())
        session_start_text = session_start_out["hookSpecificOutput"]["additionalContext"]

        # A file seed matching the fixture graph's `trader/exec.py` -- with NO seed at
        # all, get_task_context has nothing task-relevant to anchor to and renders the
        # generic "No context found." for every store, which would guard nothing.
        task_context = server_module._get_task_context_impl(
            store, reader, ["trader/exec.py"], None, 4000, 6000
        )
        toc = render_toc(build_toc(store))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.stdin = saved_stdin
        hooks_module.datetime = saved_hooks_datetime
        retrieval_module.datetime = saved_retrieval_datetime

    queue_counts = list(store.pending_ratification_counts())

    return {
        "task_context": task_context,
        "toc": toc,
        "session_start": session_start_text,
        "queue_counts": queue_counts,
    }


def test_manual_render_parity_goldens_are_frozen(tmp_path):
    """Guard against silent golden drift: rebuilding the T9 fixture on THIS revision
    (policy manual -- the only policy this task's code can produce, since no
    auto-ratification behavior exists yet) and re-rendering the four surfaces under a
    freshly-frozen clock must match the committed
    ``tests/fixtures/auto_policy/parity_goldens.json`` byte-for-byte, regardless of the
    real calendar date this test happens to run on (see ``_render_parity_surfaces``'s
    docstring for why the clock is frozen). This is Task 1's own guard that the frozen
    file matches its own builder; Task 7 owns the actual manual-vs-auto-code and
    real-vs-shifted-clock parity assertions (spec T9) once the auto-ratify machinery
    exists to threaten them.
    """
    now = datetime.now(UTC)
    store = _golden_store(tmp_path, now)
    reader = GraphifyReader(GOLDEN_GRAPH)
    rendered = _render_parity_surfaces(store, reader, GOLDEN_GRAPH, now=now)
    frozen = json.loads(GOLDENS.read_text())
    assert rendered == frozen


# ── Task 2: eligibility predicate (D3 gates) ─────────────────────────────────────────
#
# Pure predicate tests only -- no store, no reader. `_sig()` builds an `AutoEligibility`
# that is eligible under `auto-low-risk` by default (every gate individually satisfied);
# each test flips exactly the field(s) its gate cares about so a failure points straight
# at the broken gate. Per-caller adapter truthfulness (does `ProposeResult` really map to
# a `pipeline_clean=True` signal when the write succeeded?) is Tasks 4-5's job via e2e
# tests that force each caller's REAL verdict false -- these tests alone prove nothing
# about wiring, only about the predicate's own logic.


def _sig(**kw):
    base = dict(
        kind="gotcha",
        live_tier12=1,
        ambiguous_or_orphan_only=False,
        pipeline_clean=True,
        has_provenance=True,
        domain_anchored=False,
        has_supersedes=False,
    )
    base.update(kw)
    return capture.AutoEligibility(**base)


def test_manual_policy_never_eligible():
    """D1/D3 gate 1: `manual` is the default and a zero-transition path -- it must
    reject an otherwise-fully-eligible signal of every admissible shape (a low-risk
    decision, an `auto-all`-only decision, and a clean domain), not just fall through to
    `False` for some unrelated reason. Without this test the policy guard itself
    (`policy not in (AUTO_LOW_RISK, AUTO_ALL)`) is unguarded: deleting it routes `manual`
    into the `auto-all` branch and every other test here still passes `AUTO_LOW_RISK`/
    `AUTO_ALL` explicitly, so none of them would notice."""
    assert auto_ratify_eligible(_sig(), RatifyPolicy.MANUAL) is False
    assert auto_ratify_eligible(_sig(kind="adr"), RatifyPolicy.MANUAL) is False
    clean_domain = _sig(
        kind="domain", domain_anchored=True, live_tier12=0, ambiguous_or_orphan_only=True
    )
    assert auto_ratify_eligible(clean_domain, RatifyPolicy.MANUAL) is False


def test_low_risk_admits_gotcha_with_live_tier1():
    """T2 predicate half: `auto-low-risk` admits exactly the kinds D3 gate 1 names --
    `gotcha`/`lesson` decisions and standalone facts -- when every other gate is clean."""
    assert auto_ratify_eligible(_sig(kind="gotcha"), RatifyPolicy.AUTO_LOW_RISK) is True
    assert auto_ratify_eligible(_sig(kind="lesson"), RatifyPolicy.AUTO_LOW_RISK) is True
    assert auto_ratify_eligible(_sig(kind="fact"), RatifyPolicy.AUTO_LOW_RISK) is True


def test_low_risk_rejects_adr_with_identical_gates():
    """T3 predicate half: the kind gate, not some other field, is what blocks `adr`/
    `constraint` under `auto-low-risk` -- every other gate is identical to the admitted
    case above. The `auto-all` control on the same signal proves it really is the
    low-risk-vs-all boundary doing the work, not an unrelated bug that happens to reject
    everything."""
    assert auto_ratify_eligible(_sig(kind="adr"), RatifyPolicy.AUTO_LOW_RISK) is False
    assert auto_ratify_eligible(_sig(kind="constraint"), RatifyPolicy.AUTO_LOW_RISK) is False
    assert auto_ratify_eligible(_sig(kind="adr"), RatifyPolicy.AUTO_ALL) is True
    assert auto_ratify_eligible(_sig(kind="constraint"), RatifyPolicy.AUTO_ALL) is True


def test_orphan_only_never_eligible():
    """T4 predicate half. The real product rule (D3 gate 2) is the FIRST and THIRD cases
    below: zero live Tier-1/2 bindings never auto-ratify, regardless of what the
    `ambiguous_or_orphan_only` flag says -- "orphan-only" describes WHY there are zero
    live bindings, it is not a second independent condition.

    The MIDDLE case (`live_tier12=1, ambiguous_or_orphan_only=True`) is different and
    must not be read as a product decision: by contract these two fields describe the
    SAME fact from two angles, so a positive live count together with
    `ambiguous_or_orphan_only=True` is a CONTRADICTORY input that a correct adapter never
    produces (a record with one live Tier-1/2 binding and one merely ambiguous/orphaned
    OTHER anchor IS anchored under D3 -- an ambiguous anchor is a MISSING binding, not a
    wrong one). This assertion pins the predicate's DEFENSIVE behaviour on that
    contradictory input -- `and not ambiguous_or_orphan_only` stays in the conjunction as
    a guard against a mis-computed `live_tier12`/`ambiguous_or_orphan_only` pair, never
    as a statement that a genuinely mixed-anchor record is ineligible (review round 3,
    Important 1 / Ruling N). Checked under both auto policies."""
    for policy in (RatifyPolicy.AUTO_LOW_RISK, RatifyPolicy.AUTO_ALL):
        kind = "gotcha" if policy is RatifyPolicy.AUTO_LOW_RISK else "adr"
        # Zero live bindings, anchors not flagged ambiguous/orphan-only -- the real rule.
        assert auto_ratify_eligible(_sig(kind=kind, live_tier12=0), policy) is False
        # Contradictory input: a live binding exists, but the flag ALSO claims
        # orphan/ambiguous-only. Defensive rejection, not a mixed-anchor product rule.
        assert (
            auto_ratify_eligible(
                _sig(kind=kind, live_tier12=1, ambiguous_or_orphan_only=True), policy
            )
            is False
        )
        # Both signals agree there is nothing live to stand on.
        assert (
            auto_ratify_eligible(
                _sig(kind=kind, live_tier12=0, ambiguous_or_orphan_only=True), policy
            )
            is False
        )


def test_unclean_verdict_never_eligible():
    """T5 predicate half: a non-`written` pipeline verdict (deduped/skipped/dry-run)
    blocks auto-ratify regardless of policy, even when every other gate is clean."""
    unclean_gotcha = _sig(kind="gotcha", pipeline_clean=False)
    unclean_adr = _sig(kind="adr", pipeline_clean=False)
    assert auto_ratify_eligible(unclean_gotcha, RatifyPolicy.AUTO_LOW_RISK) is False
    assert auto_ratify_eligible(unclean_adr, RatifyPolicy.AUTO_ALL) is False


def test_low_risk_rejects_superseding_draft():
    """T12 predicate half: under `auto-low-risk`, `has_supersedes=True` blocks eligibility
    even for an otherwise-eligible `gotcha` -- the blast radius of a supersession is the
    PREDECESSOR's kind, so a low-risk draft must not close a human-ratified record
    unattended. Under `auto-all` the identical superseding signal is admitted BY SHAPE
    (D2 still defers the predecessor's close to a successful `Store.ratify`) -- the
    contrast proves this is a low-risk-specific gate, not a blanket supersedes ban.

    D3 gate 1 names "decisions AND facts" -- checked for `gotcha` AND `lesson` AND
    `fact` (the whole `auto-low-risk` kind set) so an implementation that exempts facts
    (D2's cascade rule needs this half: a superseding standalone fact must also stay
    proposed under `auto-low-risk`) cannot pass by covering decisions alone."""
    for kind in ("gotcha", "lesson", "fact"):
        assert (
            auto_ratify_eligible(_sig(kind=kind, has_supersedes=True), RatifyPolicy.AUTO_LOW_RISK)
            is False
        ), kind
    assert (
        auto_ratify_eligible(_sig(kind="gotcha", has_supersedes=True), RatifyPolicy.AUTO_ALL)
        is True
    )
    assert (
        auto_ratify_eligible(_sig(kind="adr", has_supersedes=True), RatifyPolicy.AUTO_ALL) is True
    )


def test_domain_needs_auto_all_and_anchor():
    """T13 predicate half: a domain candidate is eligible iff policy is `auto-all` AND
    `domain_anchored` -- never under `auto-low-risk` regardless of anchor state, and never
    under `auto-all` without a clean anchor. `live_tier12=0` and
    `ambiguous_or_orphan_only=True` here match what a REAL domain signal looks like
    (domains carry no `AnchorBinding` at propose time -- D3 says the field is 0 for them
    "by construction") and prove the domain branch does not also gate on the
    decision/fact anchor fields."""
    for policy in (RatifyPolicy.AUTO_LOW_RISK, RatifyPolicy.AUTO_ALL):
        for domain_anchored in (True, False):
            expected = policy is RatifyPolicy.AUTO_ALL and domain_anchored
            signal = _sig(
                kind="domain",
                live_tier12=0,
                ambiguous_or_orphan_only=True,
                domain_anchored=domain_anchored,
            )
            assert auto_ratify_eligible(signal, policy) is expected, (policy, domain_anchored)

    # D3 gate 3 (pipeline verdict) is the whole mechanism behind T16 ("dry run is a
    # zero-transition path") for domains -- an otherwise-clean, well-anchored domain
    # must still stay proposed when its own write was not a clean, non-dry-run write.
    dirty_domain = _sig(
        kind="domain",
        domain_anchored=True,
        pipeline_clean=False,
        live_tier12=0,
        ambiguous_or_orphan_only=True,
    )
    assert auto_ratify_eligible(dirty_domain, RatifyPolicy.AUTO_ALL) is False


def test_provenance_gate_blocks_decision_and_domain():
    """D3 gate 4: provenance is a write invariant already, but this predicate restates
    it, and the restatement itself has to be exercised -- both the decision/fact
    terminal `return bool(signal.has_provenance)` and the domain branch's own copy are
    separate lines of code that each need a signal reaching them with
    `has_provenance=False`."""
    assert auto_ratify_eligible(_sig(has_provenance=False), RatifyPolicy.AUTO_LOW_RISK) is False
    unprovenanced_domain = _sig(
        kind="domain",
        domain_anchored=True,
        has_provenance=False,
        live_tier12=0,
        ambiguous_or_orphan_only=True,
    )
    assert auto_ratify_eligible(unprovenanced_domain, RatifyPolicy.AUTO_ALL) is False


def test_predicate_never_raises_on_nonsense_input():
    """The predicate is total: an unrecognized `kind`, a negative/nonsensical anchor
    count, and contradictory flags (both `pipeline_clean` and
    `ambiguous_or_orphan_only`, or a domain-shaped signal with a `live_tier12` value
    that could never occur for a real domain and no `domain_anchored`) must all resolve
    to `False` rather than raise, under every policy including `manual`."""
    nonsense_signals = [
        _sig(kind="not-a-real-kind"),
        _sig(kind="gotcha", live_tier12=-7),
        _sig(
            kind="gotcha",
            pipeline_clean=True,
            ambiguous_or_orphan_only=True,
            has_supersedes=True,
        ),
        # A domain's `live_tier12` is 0 "by construction" (D3) -- a nonzero value here
        # is nonsensical input, paired with `domain_anchored=False` so this stays
        # ineligible under every policy rather than accidentally probing a real gate.
        _sig(kind="domain", domain_anchored=False, has_supersedes=True, live_tier12=99),
        capture.AutoEligibility(
            kind=None,  # type: ignore[arg-type]
            live_tier12=-999,
            ambiguous_or_orphan_only=True,
            pipeline_clean=False,
            has_provenance=False,
            domain_anchored=True,
            has_supersedes=True,
        ),
    ]
    for policy in (RatifyPolicy.MANUAL, RatifyPolicy.AUTO_LOW_RISK, RatifyPolicy.AUTO_ALL):
        for signal in nonsense_signals:
            assert auto_ratify_eligible(signal, policy) is False


# ── Task 3: explicit auto stamp through the existing transitions (spec D2 stamp half,
# T2 stamp half, T17) ─────────────────────────────────────────────────────────────────


def _proposed_fact(store, statement="a fact", supports=None):
    """Minimal store-level PROPOSED :class:`Fact`, mirroring ``_proposed()``'s shape
    (test-inventory.md scaffolding) for the fact half of the stamp/cascade tests below."""
    f = Fact(
        statement=statement,
        source="benchmark run",
        supports=supports or [],
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    return store.add_fact(f)


def _proposed_domain(store, slug="trading"):
    """Minimal store-level PROPOSED :class:`Domain`, mirroring ``_proposed()``'s shape
    for the domain half of the stamp tests below."""
    d = Domain(
        slug=slug,
        title="Trading",
        summary="Order execution path.",
        provenance=Provenance(source="agent"),
    )
    return store.add_domain(d)


def test_auto_stamp_flows_through_ratify(store):
    """T2 stamp half: an explicit ``actor`` passed to ``Store.ratify`` lands verbatim in
    ``ratified_by``, and ``ratified_at`` is still stamped alongside it — the auto-ratify
    caller's ``f"auto:{policy.value}"`` reaching the canonical record through the SAME
    transition a human tap uses (design D2)."""
    d = _proposed(store, "auto me")
    out, _ = store.ratify(d.id, actor="auto:auto-low-risk")
    assert out.ratified_by == "auto:auto-low-risk"
    assert out.ratified_at is not None


def test_explicit_actor_is_stored_verbatim(store):
    """A second, independently-chosen explicit actor is stored exactly as given — guards
    against an implementation that special-cases the ``"auto:auto-low-risk"`` string from
    the test above rather than forwarding whatever ``actor`` it is actually given."""
    d = _proposed(store, "explicit")
    out, _ = store.ratify(d.id, actor="auto:auto-all")
    assert out.ratified_by == "auto:auto-all"


def test_blank_actor_falls_back_to_identity(store, monkeypatch):
    """T17: an empty or whitespace-only ``actor`` is treated as absent, not stored — it
    falls back to ``_ratifier_identity``'s own best-effort ``git config user.name``
    lookup (``store.py:528-535`` pre-Task-3). The git call itself is stubbed here, NOT
    ``_ratifier_identity`` — the real blank-vs-non-blank branch inside that function
    still runs, which is the point.

    The rev-4 form of this assertion, ``(out.ratified_by or "") != "  "``, PASSES against
    an implementation that stores ``""`` verbatim: ``"" or ""`` is falsy, so the
    comparison reduces to ``"" != "  "``, true regardless of what was actually stored —
    it could not see the defect it claimed to guard. This version pins the exact fallback
    value instead, so storing ``""`` (or leaving ``"  "`` untouched) fails the assertion.
    """
    import subprocess
    import types

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="Test Person\n", returncode=0),
    )
    for blank in ("", "  "):
        d = _proposed(store, f"blank actor {blank!r}")
        out, _ = store.ratify(d.id, actor=blank)
        assert out.ratified_by == "Test Person"  # exact fallback, never "" / "  "


def test_blank_actor_with_no_identity_stamps_none(store, monkeypatch):
    """T17's second half: when a blank actor's fallback (the git lookup) ALSO yields
    nothing, the contract is ``None`` — never a blank string either way, and never the
    literal blank ``actor`` value that was passed in."""
    import subprocess
    import types

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="", returncode=0)
    )
    d = _proposed(store, "no identity")
    out, _ = store.ratify(d.id, actor="")
    assert out.ratified_by is None


def test_blank_actor_falls_back_to_identity_via_ratify_fact(store, monkeypatch):
    """Fix round 1, Promoted Minor 2: the blank-guard is centralized in
    ``_ratifier_identity``, and every call site independently *chooses* to forward into
    it — so the guard being right for ``Store.ratify`` (the test above) does not prove
    ``ratify_fact`` also routes a blank actor through it rather than, say, writing it
    verbatim. Same shape as the test above, over the standalone-fact transition."""
    import subprocess
    import types

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="Test Person\n", returncode=0),
    )
    for blank in ("", "  "):
        fact = _proposed_fact(store, f"blank actor fact {blank!r}")
        out = store.ratify_fact(fact.id, actor=blank)
        assert out.ratified_by == "Test Person"  # exact fallback, never "" / "  "


def test_blank_actor_falls_back_to_identity_via_ratify_domains(store, monkeypatch):
    """Fix round 1, Promoted Minor 2: same property, over the bulk domain transition —
    a call site there could just as easily forward a blank ``actor`` verbatim instead of
    routing it through ``_ratifier_identity``'s guard."""
    import subprocess
    import types

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="Test Person\n", returncode=0),
    )
    for i, blank in enumerate(("", "  ")):
        domain = _proposed_domain(store, slug=f"blank-actor-domain-{i}")
        result = store.ratify_domains(accept=[domain.domain_id], actor=blank)
        assert result == {domain.domain_id: "accepted"}
        accepted = store.get_domain(domain.domain_id)
        assert accepted.ratified_by == "Test Person"  # exact fallback, never "" / "  "


def test_auto_stamp_cascades_to_attached_facts_via_ratify(store):
    """Design D2's cascade rule + T11/T14: a fact attached to the decision (its
    ``supports`` names the decision) rides the SAME explicit ``actor`` stamp the decision
    itself gets, via ``Store.ratify``'s existing cascade loop — the nested fact result
    must tell the same truth as the canonical decision row (spec D6), not silently fall
    back to the git identity or stay ``None``."""
    d = _proposed(store, "decision with a rider")
    fact = _proposed_fact(store, "a rider fact", supports=[d.id])
    _, cascaded = store.ratify(d.id, actor="auto:auto-low-risk")
    assert [f.id for f in cascaded] == [fact.id]
    assert cascaded[0].ratified_by == "auto:auto-low-risk"
    assert store.get_fact(fact.id).ratified_by == "auto:auto-low-risk"


def test_auto_stamp_flows_through_ratify_fact(store):
    """The standalone-fact transition gets the same explicit-actor capability as
    ``Store.ratify`` — a fact with no ``supports`` ratified directly, not via cascade."""
    fact = _proposed_fact(store, "a standalone fact")
    out = store.ratify_fact(fact.id, actor="auto:auto-low-risk")
    assert out.ratified_by == "auto:auto-low-risk"
    assert out.ratified_at is not None


def test_auto_stamp_flows_through_ratify_domains(store):
    """The bulk domain transition gets the same explicit-actor capability: an accepted
    domain is stamped with the given ``actor`` rather than the git identity."""
    domain = _proposed_domain(store)
    result = store.ratify_domains(accept=[domain.domain_id], actor="auto:auto-all")
    assert result == {domain.domain_id: "accepted"}
    accepted = store.get_domain(domain.domain_id)
    assert accepted.status == DomainStatus.ACCEPTED
    assert accepted.ratified_by == "auto:auto-all"


def test_actor_is_keyword_only_on_all_three_transitions():
    """Design D2/T17 contract: ``actor`` is keyword-only on ``ratify``/``ratify_fact``/
    ``ratify_domains`` — a positional third argument can never be silently misread as it
    (there is no other positional parameter on any of the three it could collide with
    today, but the contract is explicit in the spec and worth pinning directly)."""
    import inspect

    for method in (Store.ratify, Store.ratify_fact, Store.ratify_domains):
        assert inspect.signature(method).parameters["actor"].kind == (
            inspect.Parameter.KEYWORD_ONLY
        )


# ── Task 4: wire capture (all three propose paths) + shared domain activation ────────
#
# End-to-end tests only, over a REAL `_feature_graph` reader (spec T2/T5/T6/T11-T18
# capture halves) -- `reader=None` orphans every anchor and nothing is ever eligible, so
# a positive test needs the real reader to force a genuine "written"/"proposed" verdict
# out of the pipeline, never a hand-built dataclass.


def test_capture_auto_ratifies_eligible_gotcha(store, tmp_path):
    """T2/T5 capture half: a real `capture.propose` call over a real reader, under
    `auto-low-risk` -- BOTH the canonical `Decision` and the returned `ProposeResult`
    carry the `"auto:auto-low-risk"` stamp, and `auto_ratify_error` is `None`."""
    reader = _feature_graph(tmp_path)
    [result] = capture.propose(
        [_eligible_decision_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert result.status == "written"
    assert result.ratified_by == "auto:auto-low-risk"
    assert result.auto_ratify_error is None
    stored = store.get_decision(result.decision_id)
    assert stored.status == DecisionStatus.ACCEPTED
    assert stored.ratified_by == "auto:auto-low-risk"
    assert stored.ratified_at is not None


def test_capture_manual_leaves_proposed(store, tmp_path):
    """`manual` (the default) is a zero-transition path even for an otherwise-fully-
    eligible draft over a real reader — `ratified_by`/`auto_ratify_error` both stay
    `None`, and the canonical record is untouched at `proposed`."""
    reader = _feature_graph(tmp_path)
    [result] = capture.propose([_eligible_decision_draft()], store, reader)
    assert result.ratified_by is None
    assert result.auto_ratify_error is None
    stored = store.get_decision(result.decision_id)
    assert stored.status == DecisionStatus.PROPOSED
    assert stored.ratified_by is None


def test_gotcha_with_one_ambiguous_anchor_still_auto_ratifies(store, tmp_path):
    """External review finding (D3 gate 2): `ambiguous_or_orphan_only` means "no live
    Tier-1/2 binding to stand on", NOT "some anchor was ambiguous" — computing it as
    `bool(anchors_skipped or anchors_orphaned)` is the natural-looking mistake, and it is
    wrong. A draft with one anchor that resolves (`Trader`) and one that is ambiguous
    (`retry()`, `_feature_graph`'s duplicated pair) IS anchored under D3 and must still
    auto-ratify under `auto-low-risk`."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft(
        anchors=[
            {"name": "Trader", "file_path": "trader/exec.py"},
            {"name": "retry()"},
        ]
    )
    [result] = capture.propose([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK)
    assert result.status == "written"
    assert result.anchors_skipped  # the ambiguous anchor IS reported back...
    assert result.ratified_by == "auto:auto-low-risk"  # ...but never blocks eligibility
    assert store.get_decision(result.decision_id).status == DecisionStatus.ACCEPTED


def test_capture_standalone_fact_auto_ratifies(store, tmp_path):
    """T2/T5 capture half, standalone side: `capture.propose_facts` (never
    `_propose_one`'s facts loop) routes an eligible standalone fact through
    `_auto_ratify(..., "fact", ...)` -> `Store.ratify_fact` — NOT `Store.ratify`, which
    would raise "is not proposed" against a fact id (`get_decision` returns `None` for
    one) and get silently swallowed into `auto_ratify_error` by `_auto_ratify`'s own
    exception handling, with no OTHER test in this module able to see the difference."""
    reader = _feature_graph(tmp_path)
    [result] = capture.propose_facts(
        [_eligible_fact_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert result.status == "written"
    assert result.ratified_by == "auto:auto-low-risk"
    assert result.auto_ratify_error is None
    stored = store.get_fact(result.fact_id)
    assert stored.status == DecisionStatus.ACCEPTED
    assert stored.ratified_by == "auto:auto-low-risk"


def test_deduped_draft_never_auto(store, tmp_path):
    """The dedup early-return (`_is_duplicate`) happens BEFORE `store.add_decision` — a
    deduped draft never reaches the auto-ratify block at all, regardless of policy."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft()
    first, second = capture.propose(
        [draft, draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert first.status == "written"
    assert first.ratified_by == "auto:auto-low-risk"
    assert second.status == "deduped"
    assert second.ratified_by is None
    assert second.auto_ratify_error is None


def test_attached_fact_rides_gotcha_cascade(store, tmp_path):
    """D2 cascade rule: an eligible gotcha's attached fact is never independently
    evaluated, but rides `Store.ratify`'s cascade with the SAME stamp — the canonical
    `Fact` row AND the nested `ProposeFactResult` both tell the same truth (T11/T14)."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft(facts=[_eligible_fact_draft()])
    [result] = capture.propose([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK)
    assert result.ratified_by == "auto:auto-low-risk"
    [fact_result] = result.facts
    assert fact_result.status == "written"
    assert fact_result.ratified_by == "auto:auto-low-risk"
    assert fact_result.auto_ratify_error is None
    stored_fact = store.get_fact(fact_result.fact_id)
    assert stored_fact.status == DecisionStatus.ACCEPTED
    assert stored_fact.ratified_by == "auto:auto-low-risk"


def test_attached_fact_under_adr_stays_proposed_low_risk(store, tmp_path):
    """`kind="adr"` isn't low-risk-admitted (D3 gate 1), so the decision's own auto-block
    never fires under `auto-low-risk` — its attached fact rides no cascade either
    (attached facts are never independently eligible) and stays proposed alongside it."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft(kind="adr", facts=[_eligible_fact_draft()])
    [result] = capture.propose([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK)
    assert result.status == "written"
    assert result.ratified_by is None
    assert result.auto_ratify_error is None
    [fact_result] = result.facts
    assert fact_result.ratified_by is None
    assert store.get_decision(result.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.PROPOSED


def test_attached_fact_with_own_ambiguous_anchors_blocks_decision(store, tmp_path):
    """T11 third clause (design D2 cascade rule): an attached fact's OWN anchors override
    the inherited decision anchors entirely (`_propose_fact_one`'s reachability step) —
    when its own anchor is ambiguous-only, the fact fails the cascade's fact-half gate 2,
    and the OWNING DECISION's auto-block is skipped entirely: decision AND fact stay
    proposed together, to leave the queue by one human verdict."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft(facts=[_eligible_fact_draft(anchors=[{"name": "retry()"}])])
    [result] = capture.propose([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK)
    assert result.ratified_by is None
    assert result.auto_ratify_error is None
    [fact_result] = result.facts
    assert fact_result.status == "written"
    assert fact_result.ratified_by is None
    assert store.get_decision(result.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.PROPOSED


def test_manual_supersession_keeps_eager_close(store, tmp_path):
    """T12: `manual` (the default) keeps today's eager close — the predecessor is
    superseded immediately at write time, unaffected by the deferred-supersession
    machinery this task adds for the two auto policies."""
    reader = _feature_graph(tmp_path)
    [first] = capture.propose([_eligible_decision_draft(title="predecessor")], store, reader)
    predecessor_id = first.decision_id
    [second] = capture.propose(
        [_eligible_decision_draft(title="successor", supersedes=predecessor_id)], store, reader
    )
    assert second.ratified_by is None
    assert store.get_decision(predecessor_id).status == DecisionStatus.SUPERSEDED
    assert store.get_decision(second.decision_id).status == DecisionStatus.PROPOSED


def test_superseding_gotcha_stays_proposed_low_risk(store, tmp_path):
    """T12: under `auto-low-risk`, gate 1 blocks a superseding gotcha draft outright
    (`has_supersedes=True`) — the successor stays proposed, and because
    `close_predecessor` was correctly DEFERRED at write time, the predecessor is left
    exactly as it was (never closed by an ineligible proposal nobody has reviewed yet)."""
    reader = _feature_graph(tmp_path)
    [predecessor_result] = capture.propose(
        [_eligible_decision_draft(title="predecessor")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    predecessor_id = predecessor_result.decision_id
    assert store.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED

    [successor] = capture.propose(
        [_eligible_decision_draft(title="successor", supersedes=predecessor_id)],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert successor.status == "written"
    assert successor.ratified_by is None
    assert store.get_decision(successor.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED  # untouched


def test_auto_all_closes_predecessor_only_after_success(store, tmp_path):
    """T12: under `auto-all`, a superseding gotcha IS eligible by shape — the close only
    takes effect once `Store.ratify` actually succeeds; it is `Store.ratify`'s OWN
    existing deferred-supersession branch that performs it, not `add_decision`."""
    reader = _feature_graph(tmp_path)
    [predecessor_result] = capture.propose(
        [_eligible_decision_draft(title="predecessor")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    predecessor_id = predecessor_result.decision_id

    [successor] = capture.propose(
        [_eligible_decision_draft(title="successor", supersedes=predecessor_id)],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert successor.ratified_by == "auto:auto-all"
    assert store.get_decision(predecessor_id).status == DecisionStatus.SUPERSEDED
    assert store.get_decision(successor.decision_id).status == DecisionStatus.ACCEPTED


def test_ineligible_auto_all_superseder_leaves_predecessor_open(store, tmp_path):
    """T12: `auto-all` admits a superseding draft's SHAPE, but it still must pass the
    remaining gates — an anchorless successor fails gate 2 and stays proposed, and the
    deferred close means the predecessor is left exactly as it was."""
    reader = _feature_graph(tmp_path)
    [predecessor_result] = capture.propose(
        [_eligible_decision_draft(title="predecessor")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    predecessor_id = predecessor_result.decision_id

    [successor] = capture.propose(
        [_eligible_decision_draft(title="successor", supersedes=predecessor_id, anchors=[])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert successor.status == "written"
    assert successor.ratified_by is None
    assert store.get_decision(successor.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED  # untouched


def test_failed_auto_all_superseder_leaves_predecessor_open(store, tmp_path, monkeypatch):
    """T12: even when every gate passes, the transition itself can still fail (e.g. lost
    a race) — the deferred close means a failed attempt leaves the predecessor exactly as
    it was, never partially superseded."""
    reader = _feature_graph(tmp_path)
    [predecessor_result] = capture.propose(
        [_eligible_decision_draft(title="predecessor")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    predecessor_id = predecessor_result.decision_id

    def failing_ratify(decision_id, *, actor=None, cascade_guard=None):
        raise ValueError("simulated race: decision already ratified")

    monkeypatch.setattr(store, "ratify", failing_ratify)

    [successor] = capture.propose(
        [_eligible_decision_draft(title="successor", supersedes=predecessor_id)],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert successor.ratified_by is None
    assert successor.auto_ratify_error == "simulated race: decision already ratified"
    assert store.get_decision(successor.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED  # untouched


def test_legacy_auto_accept_supersession_keeps_direct_precedence(store, tmp_path):
    """T12: legacy `auto_accept=True` (`SIDEGRAPH_AUTO_ACCEPT=on`) keeps its pre-existing
    direct-accepted precedence — the decision lands ACCEPTED immediately via
    `add_decision`, `close_predecessor` stays eager, and the new auto-ratify machinery
    never runs at all (it fires ONLY on writes that landed `proposed`) even when
    `ratify_policy` is also set to an auto value.

    m2 (external review): "the hook runs only on writes that landed proposed" is its own
    guard (`not auto_accept`) at BOTH the decision and the standalone/attached-fact auto
    blocks — dropping either one attempts `Store.ratify`/`ratify_fact` on an
    already-ACCEPTED record and reports a spurious `auto_ratify_error`, even though
    canonical state is unaffected (D6 truthfulness). The successor draft below carries an
    attached fact specifically so BOTH guards are exercised in the one call that actually
    combines `auto_accept=True` with a non-`manual` `ratify_policy` (the predecessor call
    uses the default `manual` policy, so its own blocks never reach the guard at all)."""
    reader = _feature_graph(tmp_path)
    [predecessor_result] = capture.propose(
        [_eligible_decision_draft(title="predecessor")], store, reader, auto_accept=True
    )
    predecessor_id = predecessor_result.decision_id
    assert store.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED
    assert predecessor_result.ratified_by is None  # accepted directly, never auto-ratified
    assert predecessor_result.auto_ratify_error is None

    [successor] = capture.propose(
        [
            _eligible_decision_draft(
                title="successor",
                supersedes=predecessor_id,
                facts=[_eligible_fact_draft()],
            )
        ],
        store,
        reader,
        auto_accept=True,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert successor.ratified_by is None
    assert successor.auto_ratify_error is None
    assert store.get_decision(successor.decision_id).status == DecisionStatus.ACCEPTED
    assert store.get_decision(predecessor_id).status == DecisionStatus.SUPERSEDED  # eager close

    [fact_result] = successor.facts
    assert fact_result.ratified_by is None
    assert fact_result.auto_ratify_error is None
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.ACCEPTED


def test_legacy_auto_accept_standalone_fact_never_auto_ratifies(store, tmp_path):
    """m2, standalone-fact half: an ATTACHED fact's own auto block is gated FIRST by
    `attached_to is None` (see `_propose_fact_one`), so it never even reaches the
    `not auto_accept` check regardless of that guard's presence — the test above,
    which supersedes a decision carrying an attached fact under legacy `auto_accept`,
    cannot exercise this guard at all. A STANDALONE fact (`capture.propose_facts`) is
    the only shape where this specific guard is load-bearing: removing
    `not auto_accept` there attempts `Store.ratify_fact` on an already-ACCEPTED fact
    and reports a spurious `auto_ratify_error`, even though canonical state is
    unaffected (D6 truthfulness)."""
    reader = _feature_graph(tmp_path)
    [result] = capture.propose_facts(
        [_eligible_fact_draft()],
        store,
        reader,
        auto_accept=True,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert result.status == "written"
    assert store.get_fact(result.fact_id).status == DecisionStatus.ACCEPTED
    assert result.ratified_by is None
    assert result.auto_ratify_error is None


@pytest.mark.parametrize("exc", [ValueError("not proposed"), RuntimeError("disk unavailable")])
def test_ratify_exception_is_reported_and_batch_continues(store, tmp_path, monkeypatch, exc):
    """T15(a/b): `_auto_ratify` catches `Exception` broadly (the anticipated `ValueError`
    race AND an unexpected `RuntimeError` alike) and never aborts the batch or turns a
    landed write into anything but `"written"` — the first draft's failed attempt reports
    itself, and the second draft still auto-accepts normally."""
    reader = _feature_graph(tmp_path)
    real_ratify = store.ratify
    calls = {"n": 0}

    def flaky_ratify(decision_id, *, actor=None, cascade_guard=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return real_ratify(decision_id, actor=actor, cascade_guard=cascade_guard)

    monkeypatch.setattr(store, "ratify", flaky_ratify)

    first, second = capture.propose(
        [
            _eligible_decision_draft(title="first"),
            _eligible_decision_draft(title="second"),
        ],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert first.status == "written"
    assert first.ratified_by is None
    assert first.auto_ratify_error == str(exc)
    assert store.get_decision(first.decision_id).status == DecisionStatus.PROPOSED

    assert second.status == "written"
    assert second.ratified_by == "auto:auto-low-risk"
    assert second.auto_ratify_error is None
    assert store.get_decision(second.decision_id).status == DecisionStatus.ACCEPTED


def test_auto_ratify_lets_system_exit_and_keyboard_interrupt_propagate(
    store, tmp_path, monkeypatch
):
    """`_auto_ratify` catches `Exception`, never `BaseException` — a `KeyboardInterrupt`
    raised inside the transition must still propagate out of the batch, not be swallowed
    into `auto_ratify_error` (design D2)."""
    reader = _feature_graph(tmp_path)

    def boom(decision_id, *, actor=None, cascade_guard=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr(store, "ratify", boom)

    with pytest.raises(KeyboardInterrupt):
        capture.propose(
            [_eligible_decision_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
        )


def test_capture_domain_auto_all_only(store, tmp_path):
    """T6/T13 capture half: `capture.propose_domains` × every policy — a domain candidate
    is eligible ONLY under `auto-all`, never `auto-low-risk` or `manual`, over the exact
    same otherwise-eligible draft."""
    reader = _feature_graph(tmp_path)
    for policy, expected_stamp in (
        (RatifyPolicy.MANUAL, None),
        (RatifyPolicy.AUTO_LOW_RISK, None),
        (RatifyPolicy.AUTO_ALL, "auto:auto-all"),
    ):
        [result] = capture.propose_domains(
            [_eligible_domain_draft(slug=f"trading-{policy.value}")],
            store,
            reader,
            ratify_policy=policy,
        )
        assert result.status == "proposed"
        assert result.ratified_by == expected_stamp
        expected_status = (
            DomainStatus.ACCEPTED if expected_stamp is not None else DomainStatus.PROPOSED
        )
        assert store.get_domain(result.domain_id).status == expected_status


def test_domain_auto_resolves_membership_and_toc(store, tmp_path, monkeypatch):
    """Design D2: a successful domain auto-ratify also runs the shared activation step
    immediately (membership is non-empty, not left `[]` for the next sync), and
    `capture.propose_domains` rebuilds the TOC cache ONCE per batch, never once per
    domain (a spy on `capture.build_toc`, not `sync`'s own copy of the concept — the
    once-per-batch rebuild is capture's own responsibility, per the extraction
    contract)."""
    reader = _feature_graph(tmp_path)
    build_toc_calls: list[int] = []
    real_build_toc = capture.build_toc

    def spy_build_toc(store_):
        build_toc_calls.append(1)
        return real_build_toc(store_)

    monkeypatch.setattr(capture, "build_toc", spy_build_toc)

    drafts = [
        _eligible_domain_draft(
            slug=f"trading-{i}", seed_anchors=[{"name": "Trader", "file_path": "trader/exec.py"}]
        )
        for i in range(3)
    ]
    results = capture.propose_domains(drafts, store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    for result in results:
        assert result.ratified_by == "auto:auto-all"
        assert result.auto_ratify_error is None
        domain = store.get_domain(result.domain_id)
        assert domain.status == DomainStatus.ACCEPTED
        assert domain.communities  # resolved immediately, not left [] for the next sync

    assert len(build_toc_calls) == 1  # once per batch, never once per domain

    toc = json.loads(store.get_meta(capture.TOC_CACHE_KEY))
    slugs = {d["slug"] for d in toc["domains"]}
    assert {f"trading-{i}" for i in range(3)} <= slugs


def test_domain_refresh_failure_sets_stale_flag_and_reports(store, tmp_path, monkeypatch):
    """T15(d): reader present, but `sync.refresh_domain_communities_now` raises — the
    domain's own transition still succeeded (accepted, stamped), the activation failure
    is reported as `"activation: <reason>"`, and the pre-existing heal flag is set."""
    import sidegraph.sync as sync_mod
    from sidegraph.store import VOLATILE_STALE_KEY

    reader = _feature_graph(tmp_path)
    store.set_meta(VOLATILE_STALE_KEY, "0")
    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("resolve blew up")),
    )

    [result] = capture.propose_domains(
        [_eligible_domain_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL
    )
    assert result.ratified_by == "auto:auto-all"  # the transition itself succeeded
    assert result.auto_ratify_error == "activation: resolve blew up"
    assert store.get_domain(result.domain_id).status == DomainStatus.ACCEPTED
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_domain_transition_error_outcome_is_reported(store, tmp_path, monkeypatch):
    """T15(c): `store.ratify_domains` never raises for a bad id — it RETURNS an
    `"error: ..."` outcome string. `_auto_ratify` must normalize that returned failure the
    same way it normalizes a raised `ValueError`: the domain stays proposed,
    `ratified_by` stays `None`, and the exact outcome string lands in
    `auto_ratify_error`."""
    reader = _feature_graph(tmp_path)

    def fake_ratify_domains(accept=None, drop=None, *, actor=None):
        return {(accept or [])[0]: "error: domain vanished mid-flight"}

    monkeypatch.setattr(store, "ratify_domains", fake_ratify_domains)

    [result] = capture.propose_domains(
        [_eligible_domain_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL
    )
    assert result.status == "proposed"
    assert result.ratified_by is None
    assert result.auto_ratify_error == "error: domain vanished mid-flight"
    assert store.get_domain(result.domain_id).status == DomainStatus.PROPOSED


def test_domain_lint_failures_stay_proposed(store, tmp_path):
    """D3 domain gate: lint warnings (dead prefix, sibling-subsumption), a missing
    reader, and an unresolvable seed anchor each independently keep an
    otherwise-plausible domain candidate proposed under `auto-all` — "non-empty
    `path_prefixes`" alone is never eligibility (record `01KYSFQ5D45SBQZ238NP8Z4YWH`),
    and neither is "has a `seed_anchor`" alone (I3, external review: the anchor must
    actually RESOLVE)."""
    reader = _feature_graph(tmp_path)

    # (a) dead prefix: matches no file in the graph.
    [dead] = capture.propose_domains(
        [_eligible_domain_draft(slug="dead-prefix", seed_anchors=[], path_prefixes=["nowhere/"])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert dead.ratified_by is None
    assert store.get_domain(dead.domain_id).status == DomainStatus.PROPOSED

    # (b) sibling subsumption: an existing ACCEPTED domain's seed anchor sits under the
    # new draft's proposed path_prefix.
    [existing] = capture.propose_domains(
        [_eligible_domain_draft(slug="siblings-existing")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert existing.ratified_by == "auto:auto-all"  # accepted, seed_anchor at trader/exec.py
    [subsuming] = capture.propose_domains(
        [_eligible_domain_draft(slug="subsuming", seed_anchors=[], path_prefixes=["trader/"])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert subsuming.ratified_by is None
    assert store.get_domain(subsuming.domain_id).status == DomainStatus.PROPOSED

    # (c) reader absent: an otherwise-eligible domain (real seed anchor, no path_prefixes)
    # is never eligible without a reader at all (D3: no anchor signal without one).
    [no_reader] = capture.propose_domains(
        [_eligible_domain_draft(slug="no-reader")], store, None, ratify_policy=RatifyPolicy.AUTO_ALL
    )
    assert no_reader.ratified_by is None
    assert store.get_domain(no_reader.domain_id).status == DomainStatus.PROPOSED

    # (d) unresolvable seed anchor, no path_prefixes to fall back on: a typo'd or
    # deleted symbol must not self-certify an empty domain (I3, external review) —
    # "a seed_anchor resolves to a live node" means it actually resolves, not merely
    # that one was supplied.
    [unresolvable] = capture.propose_domains(
        [
            _eligible_domain_draft(
                slug="unresolvable-seed",
                seed_anchors=[{"name": "totally-nonexistent-symbol"}],
            )
        ],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert unresolvable.ratified_by is None
    assert store.get_domain(unresolvable.domain_id).status == DomainStatus.PROPOSED


def test_graph_json_byte_identical_around_domain_auto(store, tmp_path):
    """Global Constraints: `graph.json` is read-only input and must remain byte-identical
    across every flow — an `auto-all` domain batch that resolves membership immediately
    must never write to it."""
    reader = _feature_graph(tmp_path)
    graph_path = reader.path
    before = graph_path.read_bytes()
    capture.propose_domains(
        [_eligible_domain_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL
    )
    after = graph_path.read_bytes()
    assert before == after


def test_wrappers_delegate_to_sync_helper(tmp_path, monkeypatch):
    """Step B characterization (spec C-10): after extracting `sync.activate_accepted_
    domain`, BOTH the MCP ratify path (`server._ratify_impl`) and the CLI ratify path
    (`cli.ratify_main`) call through the SAME shared helper — a patch on
    `sync.refresh_domain_communities_now` intercepts either door, proving neither wrapper
    still calls its own former copy of the function."""
    import sidegraph.cli as cli_mod
    import sidegraph.sync as sync_mod
    from sidegraph.server import _ratify_impl

    calls: list[str] = []

    def fake_refresh(domain, store_, reader):
        calls.append(domain.domain_id)
        return (["c-1"], None)

    monkeypatch.setattr(sync_mod, "refresh_domain_communities_now", fake_refresh)

    mcp_store = Store(tmp_path / "mcp.db")
    mcp_domain = _proposed_domain(mcp_store, slug="mcp-domain")
    _ratify_impl(mcp_store, accept=[mcp_domain.domain_id], drop=None, reader=object())
    assert calls == [mcp_domain.domain_id]

    calls.clear()
    cli_db = tmp_path / "cli.db"
    cli_store = Store(cli_db)
    cli_domain = _proposed_domain(cli_store, slug="cli-domain")
    monkeypatch.setattr(cli_mod, "_ratify_reader", lambda *a, **k: object())
    assert cli_mod.ratify_main(["--db", str(cli_db), "--accept", cli_domain.domain_id]) == 0
    assert calls == [cli_domain.domain_id]


def test_human_domain_ratify_without_reader_sets_stale_without_error(store):
    """`sync.activate_accepted_domain`'s reader-None branch (design D2 contract,
    preserved from the pre-extraction wrapper): `resolved=False, error=None` — it must
    NEVER invent a new human-facing error where there was none before — and the
    pre-existing heal flag is still set."""
    from sidegraph.store import VOLATILE_STALE_KEY
    from sidegraph.sync import activate_accepted_domain

    domain = _proposed_domain(store, slug="no-graph-domain")
    store.set_meta(VOLATILE_STALE_KEY, "0")

    activation = activate_accepted_domain(domain, store, None)

    assert activation.resolved is False
    assert activation.overbroad is None
    assert activation.error is None
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_activate_accepted_domain_never_rebuilds_toc(store, tmp_path, monkeypatch):
    """Extraction contract (iv), spec D2: `sync.activate_accepted_domain` never rebuilds
    `TOC_CACHE_KEY` itself — the once-per-batch rebuild is exclusively the CALLER's job
    (`server.py`, `cli.py`, and capture's two auto callers), so a domain bootstrap of N
    domains never pays N `build_toc` calls. Spies on `sync.build_toc` directly (a spy on
    `capture.build_toc` would MISS a rebuild added inside the helper — different module,
    different name binding, verified by mutation) and checks both the success path
    (reader present, resolve succeeds) and the failure path (refresh raises)."""
    import sidegraph.sync as sync_mod
    from sidegraph.sync import activate_accepted_domain

    calls: list[int] = []
    real_build_toc = sync_mod.build_toc

    def spy_build_toc(store_):
        calls.append(1)
        return real_build_toc(store_)

    monkeypatch.setattr(sync_mod, "build_toc", spy_build_toc)

    reader = _feature_graph(tmp_path)
    domain = _proposed_domain(store, slug="toc-contract-ok")
    activation = activate_accepted_domain(domain, store, reader)
    assert activation.resolved is True
    assert calls == []

    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    domain2 = _proposed_domain(store, slug="toc-contract-fail")
    activation2 = activate_accepted_domain(domain2, store, reader)
    assert activation2.resolved is False
    assert calls == []


# ── Task 4 fix round 1 (external review) ─────────────────────────────────────────────


def test_cascade_blocks_when_any_fact_in_the_set_is_ineligible(store, tmp_path):
    """I1: D2's cascade rule requires EVERY fact in the cascade set to pass, not just
    the first one and not "any" of them. A gotcha with two attached facts — one
    eligible, one orphan-only — must leave the decision AND BOTH facts proposed;
    checking only `_cascade_set(...)[0]` or using `any(...)` instead of `all(...)`
    would incorrectly let the decision auto-ratify, which for real (not just in a
    mis-reported result) cascade-certifies the orphan fact too via `Store.ratify`'s own
    unconditional cascade — `_cascade_eligible` is what must stop that call from ever
    firing in the first place."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft(
        facts=[
            _eligible_fact_draft(statement="a hard-won, eligible fact"),
            _eligible_fact_draft(
                statement="an orphan fact with no live anchor",
                anchors=[{"name": "does-not-exist-anywhere"}],
            ),
        ]
    )
    [result] = capture.propose([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK)
    assert result.ratified_by is None
    assert result.auto_ratify_error is None
    good, orphan = result.facts
    assert good.status == "written"
    assert good.ratified_by is None
    assert orphan.status == "written"
    assert orphan.ratified_by is None
    assert store.get_decision(result.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_fact(good.fact_id).status == DecisionStatus.PROPOSED
    assert store.get_fact(orphan.fact_id).status == DecisionStatus.PROPOSED


def test_initiative_binding_never_masks_a_missing_live_anchor(store, tmp_path, monkeypatch):
    """I2: `_anchor_signal`'s `tier in (1, 2)` filter is what keeps a live Tier-0
    INITIATIVE binding from masking a record that has no real anchor to stand on. Every
    other test in this module runs under the autouse `_no_ambient_initiative` fixture
    (needed for determinism — without it, tests would depend on whatever branch this
    checkout happens to be on), which makes this filter's absence invisible to the full
    suite: production runs on a feature branch, where `_derive_initiative()` mints a
    live Tier-0 binding on EVERY capture. A per-test `monkeypatch.setattr` overrides the
    autouse fixture back to a real branch name here, and proves an orphan-only AND an
    ambiguous-only draft both still stay proposed despite the extra live Tier-0
    binding — dropping the tier filter would count that binding as "live" and
    self-certify both."""
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: "feature-x")
    reader = _feature_graph(tmp_path)

    orphan_only = _eligible_decision_draft(
        title="orphan only", anchors=[{"name": "does-not-exist-anywhere"}]
    )
    [orphan_result] = capture.propose(
        [orphan_only], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert orphan_result.ratified_by is None
    assert store.get_decision(orphan_result.decision_id).status == DecisionStatus.PROPOSED

    ambiguous_only = _eligible_decision_draft(title="ambiguous only", anchors=[{"name": "retry()"}])
    [ambiguous_result] = capture.propose(
        [ambiguous_only], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert ambiguous_result.ratified_by is None
    assert store.get_decision(ambiguous_result.decision_id).status == DecisionStatus.PROPOSED


def test_standalone_fact_ratify_exception_is_reported(store, tmp_path, monkeypatch):
    """I4: `_propose_fact_one`'s auto block must report a `ratify_fact` exception the
    same way the decision and domain sites already do — deleting
    `auto_ratify_error = outcome.error` there makes a raised exception
    indistinguishable from "ineligible, never attempted" (both leave `ratified_by=None,
    auto_ratify_error=None`), which is exactly the "swallowed exception with no field
    set" D2/T15 forbids. The first of two standalone facts fails; the second must still
    auto-accept normally (batch continues, T15 a/b's own property, restated here for the
    fact site)."""
    reader = _feature_graph(tmp_path)
    real_ratify_fact = store.ratify_fact
    calls = {"n": 0}

    def flaky_ratify_fact(fact_id, *, actor=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated race: fact already ratified")
        return real_ratify_fact(fact_id, actor=actor)

    monkeypatch.setattr(store, "ratify_fact", flaky_ratify_fact)

    first, second = capture.propose_facts(
        [
            _eligible_fact_draft(statement="first fact"),
            _eligible_fact_draft(statement="second fact"),
        ],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert first.status == "written"
    assert first.ratified_by is None
    assert first.auto_ratify_error == "simulated race: fact already ratified"
    assert store.get_fact(first.fact_id).status == DecisionStatus.PROPOSED

    assert second.status == "written"
    assert second.ratified_by == "auto:auto-low-risk"
    assert second.auto_ratify_error is None
    assert store.get_fact(second.fact_id).status == DecisionStatus.ACCEPTED


def test_standalone_fact_with_one_ambiguous_anchor_still_auto_ratifies(store, tmp_path):
    """m1: the mixed-anchor rule (`ambiguous_or_orphan_only` means "no live binding to
    stand on", not "some anchor was ambiguous") must hold at the standalone-fact site
    too, not just the decision site — computing the flag as
    `bool(anchors_skipped or anchors_orphaned)` there survives the full suite otherwise
    (standalone-fact twin of `test_gotcha_with_one_ambiguous_anchor_still_auto_ratifies`)."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_fact_draft(
        anchors=[
            {"name": "Trader", "file_path": "trader/exec.py"},
            {"name": "retry()"},
        ]
    )
    [result] = capture.propose_facts(
        [draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert result.status == "written"
    assert result.anchors_skipped  # the ambiguous anchor IS reported back...
    assert result.ratified_by == "auto:auto-low-risk"  # ...but never blocks eligibility
    assert store.get_fact(result.fact_id).status == DecisionStatus.ACCEPTED


def test_domain_with_only_path_prefixes_auto_ratifies(store, tmp_path):
    """m3: Task 5's bootstrap-derived domains have ONLY `path_prefixes` — no
    `seed_anchors` at all — so this clause going inert would silently stop domain
    auto-ratification from ever firing there, with no signal anywhere that it broke."""
    reader = _feature_graph(tmp_path)
    [result] = capture.propose_domains(
        [_eligible_domain_draft(slug="prefix-only", seed_anchors=[], path_prefixes=["util/"])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert result.ratified_by == "auto:auto-all"
    domain = store.get_domain(result.domain_id)
    assert domain.status == DomainStatus.ACCEPTED
    assert domain.communities  # resolved via path_prefixes alone, no seed_anchors needed


def test_rejected_attached_fact_never_gets_cascade_stamp(store, tmp_path):
    """m4: the nested-stamping filter (`fr.fact_id in cascaded_ids`) must actually
    filter — stamping every `fact_results` entry unconditionally would put the
    `auto:*` stamp on a REJECTED attached fact's result too (`fact_id=None`, never
    written, never part of the cascade), a public result that lies about a record that
    was never accepted."""
    reader = _feature_graph(tmp_path)
    draft = _eligible_decision_draft(
        facts=[_eligible_fact_draft(), _eligible_fact_draft(statement="")]
    )
    [result] = capture.propose([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK)
    assert result.ratified_by == "auto:auto-low-risk"
    good, rejected = result.facts
    assert good.status == "written"
    assert good.ratified_by == "auto:auto-low-risk"
    assert rejected.status == "rejected"
    assert rejected.fact_id is None
    assert rejected.ratified_by is None


# ---------------------------------------------------------------------------------------
# Final whole-branch review, rev 12 erratum -- I1 (Ruling GG) and I2 (Ruling HH).
# ---------------------------------------------------------------------------------------


def test_fact_supporting_a_still_proposed_decision_never_auto_ratifies(store, tmp_path):
    """I1 (Ruling GG): a fact whose `supports` names a still-`proposed` decision is
    never independently auto-eligible -- it rides that decision's verdict (its accept
    cascade, or its drop cascade), the store's own nested definition
    (`Store.pending_ratification_counts`). The reviewer's probe: an `adr` proposed under
    `auto-low-risk` stays `proposed` (kind gate); a SECOND `propose_facts` call then
    proposes a fact with a live anchor and `supports=[adr.id]`. RED at HEAD (rev-11 gate
    was `attached_to is None` alone): the fact auto-accepted and survived the human's
    later drop of the ADR it supposedly supported."""
    reader = _feature_graph(tmp_path)
    [adr_result] = capture.propose(
        [_eligible_decision_draft(kind="adr")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert adr_result.ratified_by is None  # kind gate: adr never eligible under auto-low-risk
    assert store.get_decision(adr_result.decision_id).status == DecisionStatus.PROPOSED

    [fact_result] = capture.propose_facts(
        [_eligible_fact_draft(supports=[adr_result.decision_id])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert fact_result.ratified_by is None
    assert fact_result.auto_ratify_error is None
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.PROPOSED

    store.drop(adr_result.decision_id)
    assert store.get_decision(adr_result.decision_id).status == DecisionStatus.REJECTED
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.REJECTED


def test_fact_supporting_an_accepted_decision_or_nothing_still_auto_ratifies(store, tmp_path):
    """Guard: I1's new gate keys off a `supports` target's STATUS, not its mere
    presence -- a fact supporting an ALREADY-accepted decision, and a fact with no
    `supports` at all, both still auto-ratify under `auto-low-risk` exactly as before
    (the rule must not be over-broad)."""
    reader = _feature_graph(tmp_path)
    [gotcha_result] = capture.propose(
        [_eligible_decision_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert gotcha_result.ratified_by == "auto:auto-low-risk"
    assert store.get_decision(gotcha_result.decision_id).status == DecisionStatus.ACCEPTED

    [supporting_fact] = capture.propose_facts(
        [_eligible_fact_draft(supports=[gotcha_result.decision_id])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert supporting_fact.ratified_by == "auto:auto-low-risk"
    assert store.get_fact(supporting_fact.fact_id).status == DecisionStatus.ACCEPTED

    [standalone_fact] = capture.propose_facts(
        [_eligible_fact_draft(statement="a second, unrelated hard-won fact")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert standalone_fact.ratified_by == "auto:auto-low-risk"
    assert store.get_fact(standalone_fact.fact_id).status == DecisionStatus.ACCEPTED


def test_fact_supporting_a_proposed_decision_stays_proposed_under_manual(store, tmp_path):
    """The `manual` arm of the I1 probe: with no auto policy at all, the fact stays
    `proposed` (zero transitions, as always) and is cascade-rejected by the human's
    later drop -- same end state as the auto-low-risk probe above, a guard that the new
    gate changes nothing under `manual`."""
    reader = _feature_graph(tmp_path)
    [adr_result] = capture.propose([_eligible_decision_draft(kind="adr")], store, reader)
    assert store.get_decision(adr_result.decision_id).status == DecisionStatus.PROPOSED

    [fact_result] = capture.propose_facts(
        [_eligible_fact_draft(supports=[adr_result.decision_id])], store, reader
    )
    assert fact_result.ratified_by is None
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.PROPOSED

    store.drop(adr_result.decision_id)
    assert store.get_fact(fact_result.fact_id).status == DecisionStatus.REJECTED


def test_domain_overbroad_path_rule_reports_and_stays_accepted(store, tmp_path):
    """I2 (Ruling HH): an auto-accepted domain whose path rule the claim cap rejects
    must report the SAME "path rule too broad" sentence the human MCP/CLI wrappers
    render (`server.py:1734-1740`, `cli.py:491-498`) -- not a clean, silent success.
    `_overbroad_graph_nodes` (`tests/test_server_domains.py`): `'trader'` matches 3 of
    the graph's 10 communities, over `sync._DOMAIN_CLAIM_CAP` (0.2). No seed anchors, so
    nothing rescues membership -- canonical `communities` stays `[]`, but the transition
    itself still committed (D6: accepted-but-unhealed is visible, never silent)."""
    from tests.test_server_domains import _overbroad_graph_nodes, _reader_over

    reader = _reader_over(tmp_path, _overbroad_graph_nodes())
    [result] = capture.propose_domains(
        [_eligible_domain_draft(seed_anchors=[], path_prefixes=["trader"])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert result.ratified_by == "auto:auto-all"
    assert result.auto_ratify_error is not None
    assert result.auto_ratify_error.startswith(
        "activation: path rule too broad: 'trader' match 3/10"
    )
    domain = store.get_domain(result.domain_id)
    assert domain.status == DomainStatus.ACCEPTED


# ---------------------------------------------------------------------------------------
# Checkpoint 2 fix regressions (external review A1/A2, Ruling Q / Ruling R) --
# scratchpad/probe_race.py and scratchpad/probe_activation.py turned into real tests.
#
# Fix round 1 (a second external review of the checkpoint-2 fix, findings I1-I5/m1) added:
# I1 (guard freshness on an ATTACHED fact, not just a newcomer), I2 (the cross-connection
# lock, not just the guard's logic, on two REAL Store connections), I3 (a refused guard
# writes nothing at the CANONICAL FILE layer, not just the derived index -- including the
# superseding-decision case, checked through a REOPENED store), I4 (a genuinely MIXED
# cascade set so `all(...)` is provably not `any(...)`), and Ruling T / I5 (the guard is no
# longer an opt-in parameter on `_auto_ratify` -- it builds one itself, so no decision
# caller, including future ones, can omit it).
# ---------------------------------------------------------------------------------------


def _canonical_snapshot(store: Store) -> dict[str, str]:
    """Sha256 of every canonical decision/fact FILE on disk, keyed by path relative to the
    store root (checkpoint-2 fix-round-1, I3): a refused `cascade_guard` must write
    NOTHING -- not just leave the derived sqlite index alone. Reading back through
    `store.get_decision`/`get_fact` only proves the INDEX rolled back; canonical files land
    first and a rollback only undoes the index (this store's own write order), so this is
    the check that actually pins "no write happened". Mirrors
    `scratchpad/probe_t2_superseder.py`'s `snap()`."""
    return {
        str(p.relative_to(store.path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for sub in ("decisions", "facts")
        for p in sorted((store.path / sub).glob("*.json"))
    }


def test_cascade_race_injected_fact_is_blocked_by_the_guard(store, tmp_path, monkeypatch):
    """A1 regression: `_propose_one`'s `_cascade_eligible` pre-check and `Store.ratify`'s
    own transition used to be split by a race window -- a fact supporting the decision
    that landed in between rode the cascade with NO eligibility re-check (external review,
    reproduced by `scratchpad/probe_race.py`). Monkeypatches `Store.ratify` at the CLASS
    level to inject an unanchored fact supporting the decision immediately before calling
    through to the real method, so `cascade_guard` (Ruling Q) actually runs on it under the
    write lock. At HEAD (before this fix) this reproduces the review's exact output:
    `decision_status=accepted`, `injected_fact_status=accepted`,
    `injected_fact_stamp=auto:auto-low-risk`, `injected_fact_live_tier12=0` -- an unanchored
    fact self-certified. After the fix: the guard refuses, and NOTHING flips.

    Fix-round-1 additions (I3/I4): the racing decision carries its OWN eligible attached
    fact, so the cascade set the guard sees is genuinely MIXED (one eligible, one not) --
    proving `all(...)`, not `any(...) `(an `any` mutant would find the eligible attached
    fact sufficient and wrongly admit the whole cascade). The refusal is also checked at
    the CANONICAL FILE layer (`_canonical_snapshot`), not just through index reads --
    catching a mutant that moves the guard check to after the writes (I3)."""
    reader = _feature_graph(tmp_path)
    injected: dict[str, str] = {}
    snapshots: dict[str, dict[str, str]] = {}
    real_ratify = Store.ratify

    def racing_ratify(self, decision_id, **kw):
        # A concurrent writer lands a fact supporting this decision, with NO anchors --
        # a distinct statement so no propose-time dedup elsewhere in the fixture could
        # ever collapse it with another fact (this build's own recorded trap).
        f = Fact(
            statement="landed by a concurrent writer mid-cascade, no anchor at all",
            source="race regression probe",
            supports=[decision_id],
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
        self.add_fact(f)
        injected["id"] = f.id
        snapshots["before"] = _canonical_snapshot(self)
        try:
            return real_ratify(self, decision_id, **kw)
        finally:
            snapshots["after"] = _canonical_snapshot(self)

    monkeypatch.setattr(Store, "ratify", racing_ratify)

    [result] = capture.propose(
        [_eligible_decision_draft(facts=[_eligible_fact_draft()])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )

    assert result.status == "written"
    assert result.ratified_by is None
    assert result.auto_ratify_error is not None
    assert "cascade guard refused" in result.auto_ratify_error
    assert store.get_decision(result.decision_id).status == DecisionStatus.PROPOSED

    # The decision's OWN attached fact (eligible by itself) must also stay blocked -- the
    # cascade rule is "every fact in the set", not "the injected one alone".
    [attached_fact_result] = result.facts
    assert attached_fact_result.ratified_by is None
    attached_fact = store.get_fact(attached_fact_result.fact_id)
    assert attached_fact.status == DecisionStatus.PROPOSED

    injected_fact = store.get_fact(injected["id"])
    assert injected_fact.status == DecisionStatus.PROPOSED
    assert injected_fact.ratified_by is None
    live = sum(
        1
        for b in store.bindings_for_record(injected_fact.id)
        if b.status == "live" and b.tier in (1, 2)
    )
    assert live == 0

    assert snapshots["before"] == snapshots["after"], (
        "canonical files changed despite the guard's refusal"
    )


def test_cascade_guard_rereads_an_attached_facts_bindings_fresh(store, tmp_path, monkeypatch):
    """I1: the guard's whole reason for existing over a compare-and-set on fact ids
    (Ruling Q) is that it re-reads each fact's bindings FRESH at call time, never a value
    computed earlier. Unlike the test above (whose injected fact is a NEWCOMER the guard
    has never seen before), this uses an ATTACHED fact -- already counted by the earlier
    `_cascade_eligible` pre-check -- and flips ITS live bindings to orphaned via a SECOND,
    independent `Store` connection between that pre-check and the guard's own re-check
    (`scratchpad/probe_t1_binding_flip.py`). A guard that cached each fact's eligibility
    the first time it was seen, re-checking only genuinely new facts, would still pass
    the test above (the injected fact IS new) but would wrongly admit this one."""
    reader = _feature_graph(tmp_path)
    real_ratify = Store.ratify
    flipped: dict[str, list[tuple[int, str]]] = {}

    def racing_ratify(self, decision_id, **kw):
        other = Store(tmp_path / "t.db")
        try:
            for f in other.iter_proposed_facts():
                if decision_id in f.supports:
                    for b in other.bindings_for_record(f.id):
                        if b.status == "live":
                            b.status = "orphaned"
                            other.add_binding(b)
                    flipped[f.id] = [(bb.tier, bb.status) for bb in other.bindings_for_record(f.id)]
        finally:
            other.close()
        return real_ratify(self, decision_id, **kw)

    monkeypatch.setattr(Store, "ratify", racing_ratify)

    [result] = capture.propose(
        [_eligible_decision_draft(facts=[_eligible_fact_draft()])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )

    assert flipped, "the attached fact's bindings were never flipped -- test setup broken"
    assert result.status == "written"
    assert result.ratified_by is None
    assert result.auto_ratify_error is not None
    assert "cascade guard refused" in result.auto_ratify_error
    assert store.get_decision(result.decision_id).status == DecisionStatus.PROPOSED

    [fact_result] = result.facts
    assert fact_result.ratified_by is None
    fact = store.get_fact(fact_result.fact_id)
    assert fact.status == DecisionStatus.PROPOSED
    live = sum(
        1 for b in store.bindings_for_record(fact.id) if b.status == "live" and b.tier in (1, 2)
    )
    assert live == 0


def test_cascade_guard_blocks_a_concurrent_writer_holding_the_lock(store, tmp_path):
    """I2: the cross-connection LOCK (`_mutation(immediate=True)`) is what closes A1 for
    real, not merely the guard's own per-fact logic. The tests above inject their fact
    from INSIDE the very call that becomes `Store.ratify` (before `real_ratify` is even
    entered), so no lock is ever contended there -- they prove the guard's logic runs, not
    that a genuinely concurrent writer on an INDEPENDENT connection is actually blocked
    while it runs.

    This opens two real `Store` connections on one directory, standing in for two
    processes -- the same load-bearing idea as
    `tests/test_entity_get_or_create_race.py::_race_two_stores` (independent connections,
    a generous `busy_timeout`, a generous bounded `join`, and an explicit
    not-still-alive assertion against a silent deadlock). It is not reused verbatim: that
    helper's marker-proxy mechanism pauses on a specific SQL statement shared by BOTH sides
    of a SYMMETRIC race (both racers run the identical get-or-create lookup, so the same
    marker text fires for either one). Here the two sides run DIFFERENT operations -- one
    ratifies under a guard, the other calls `add_fact` -- with no meaningful SQL statement
    in common to pause on; forcing that shape onto a marker either fires on a cheap read
    that returns before contention exists (a flaky, non-deterministic pause) or never fires
    at all under the very mutant being tested (an `immediate=True` that is never issued
    never executes the literal `BEGIN IMMEDIATE` text a marker could match). The guard
    callback itself is the one place both requirements are satisfiable at once: it runs
    exactly once, synchronously, at precisely the moment to test, regardless of whether the
    lock was actually taken -- the same mechanism `scratchpad/probe_t3_two_connections.py`
    uses and the one the reviewer's own measurements are against ("B landed after 1.57s"
    fixed vs. "B lands in 0.0s" under the `imm` mutant).

    When connection A's guard runs (cascade set already read, write lock already held under
    the fix), it releases connection B's `add_fact` of an unanchored fact supporting the
    decision and waits a generous, bounded window for B to finish. Under the fix, B must
    block for A's WHOLE window and its fact must land only after A commits, uncascaded.
    Dropping `immediate` (mutant `imm`) lets B land inside the window and get certified by
    A's own (unconditional, always-run) cascade write loop."""
    WAIT = 1.5
    d = store.add_decision(
        Decision(
            title="I2 lock probe",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
    )

    b_store = Store(tmp_path / "t.db")
    b_store._conn.execute("PRAGMA busy_timeout = 20000")

    go, done = threading.Event(), threading.Event()
    res: dict[str, object] = {}

    def worker() -> None:
        go.wait(10)
        t0 = time.monotonic()
        f = Fact(
            statement="unanchored, landed by a second connection mid-window",
            source="i2 lock probe",
            supports=[d.id],
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
        try:
            b_store.add_fact(f)
            res["b"] = "landed"
        except BaseException as e:  # noqa: BLE001 - captured for the assertions below
            res["b"] = f"raised {type(e).__name__}: {e}"
        res["b_secs"] = round(time.monotonic() - t0, 2)
        res["fid"] = f.id
        done.set()

    def guard(facts: object) -> bool:
        res["guard_saw"] = len(facts)  # type: ignore[arg-type]
        go.set()
        res["b_finished_inside_window"] = done.wait(WAIT)
        return True

    t = threading.Thread(target=worker)
    t.start()
    _decision, cascaded = store.ratify(d.id, actor="auto:auto-low-risk", cascade_guard=guard)
    t.join(30)
    assert not t.is_alive(), "racing thread never finished -- deadlock in the interleaving"

    assert res["b_finished_inside_window"] is False, "B finished inside A's locked window"
    assert res["b"] == "landed"
    assert not any(x.id == res["fid"] for x in cascaded), "B's fact was cascaded by A"

    fresh = Store(tmp_path / "t.db")
    try:
        landed_fact = fresh.get_fact(res["fid"])
        assert landed_fact is not None
        assert landed_fact.status == DecisionStatus.PROPOSED
        assert landed_fact.ratified_by is None
        assert fresh.get_decision(d.id).status == DecisionStatus.ACCEPTED
    finally:
        fresh.close()
    b_store.close()


def test_cascade_guard_sees_a_fact_that_lands_just_before_the_write_lock(
    store, tmp_path, monkeypatch
):
    """N1 (checkpoint-2 fix-round-2, re-review): D2 rev 8 and Ruling Q both say the
    transition takes the write lock (`BEGIN IMMEDIATE`) BEFORE reading the cascade set. No
    existing test pins that ORDERING specifically. The I1/I3 tests inject their fact from
    inside a `Store.ratify` monkeypatch that wraps the WHOLE call, so any read inside
    `real_ratify` already sees it regardless of where the read sits relative to the lock.
    The I2 test's pause point is the guard callback itself, which runs AFTER the cascade set
    is already read -- it cannot see whether that read happened before or after
    `BEGIN IMMEDIATE`. Red target `prelock`: hoist `cascade_candidates`'s computation to
    just above `with self._mutation(...)` in `Store.ratify`. Under that mutant the guard
    would see only the PRE-lock set (empty of B's fact) and admit the decision; the
    cascade write loop -- which unconditionally re-reads under the lock and is never
    itself guarded -- would then pick up B's now-committed fact and certify it for real.
    That is A1 reopened through a narrower door.

    Mirrors `scratchpad/probe_prelock.py`: connection A's own sqlite connection is wrapped
    in a proxy that fires the instant A's `BEGIN IMMEDIATE` executes (gated on
    `Store.ratify` having actually started, via the `armed` decision id -- otherwise an
    EARLIER `_mutation(immediate=True)` elsewhere in the same propose call, e.g. entity
    get-or-create during anchor resolution, could fire it too early). From there, an
    INDEPENDENT second `Store` connection commits an unanchored fact supporting the
    decision -- strictly before A's own write lock is taken. The racing decision carries
    one eligible attached fact, so the set the guard must refuse is genuinely mixed (I4).
    Ruling U: this closes N1 with a TEST ONLY -- `Store.ratify` itself is unchanged."""
    reader = _feature_graph(tmp_path)
    sdir = tmp_path / "t.db"
    injected: dict[str, str] = {}
    snapshots: dict[str, dict[str, str]] = {}
    armed: dict[str, str | None] = {"decision_id": None}

    class _PreLockInjectionProxy:
        def __init__(self, real: object) -> None:
            self._real = real
            self._fired = False

        def execute(self, sql: str, *a: object, **k: object) -> object:
            if (
                not self._fired
                and armed["decision_id"] is not None
                and sql.strip().upper() == "BEGIN IMMEDIATE"
            ):
                self._fired = True
                other = Store(sdir)
                try:
                    f = Fact(
                        statement=(
                            "unanchored, landed by a second connection just before BEGIN IMMEDIATE"
                        ),
                        source="prelock regression probe",
                        supports=[armed["decision_id"]],
                        valid_from=datetime.now(UTC),
                        provenance=Provenance(source="agent"),
                    )
                    other.add_fact(f)
                    injected["id"] = f.id
                finally:
                    other.close()
                snapshots["before"] = _canonical_snapshot(store)
            return self._real.execute(sql, *a, **k)

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    real_ratify = Store.ratify

    def arm_ratify(self: Store, decision_id: str, **kw: object):
        armed["decision_id"] = decision_id
        try:
            return real_ratify(self, decision_id, **kw)
        finally:
            snapshots["after"] = _canonical_snapshot(store)

    # Both restored automatically by the monkeypatch fixture at teardown -- including the
    # connection, an instance attribute monkeypatch tracks just as well as a class method.
    monkeypatch.setattr(store, "_conn", _PreLockInjectionProxy(store._conn))
    monkeypatch.setattr(Store, "ratify", arm_ratify)

    [result] = capture.propose(
        [_eligible_decision_draft(facts=[_eligible_fact_draft()])],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )

    assert "id" in injected, "the pre-lock injection never fired -- test setup broken"
    assert result.status == "written"
    assert result.ratified_by is None
    assert result.auto_ratify_error is not None
    assert "cascade guard refused" in result.auto_ratify_error
    assert store.get_decision(result.decision_id).status == DecisionStatus.PROPOSED

    [attached_fact_result] = result.facts
    assert attached_fact_result.ratified_by is None
    attached_fact = store.get_fact(attached_fact_result.fact_id)
    assert attached_fact.status == DecisionStatus.PROPOSED

    injected_fact = store.get_fact(injected["id"])
    assert injected_fact.status == DecisionStatus.PROPOSED
    assert injected_fact.ratified_by is None

    assert snapshots["before"] == snapshots["after"], (
        "canonical files changed despite the guard's refusal"
    )


def test_cascade_race_refusal_on_a_superseding_decision_leaves_predecessor_untouched(
    store, tmp_path, monkeypatch
):
    """I3, the superseding case: a refused `cascade_guard` on a decision that SUPERSEDES an
    accepted predecessor must leave EVERYTHING untouched -- not just the derived index. A
    mutant that moves the guard check to AFTER `_write_decision` and the predecessor close
    still satisfies an index-only check (the index rolls back on the raised `ValueError`),
    but two canonical decision FILES were already rewritten on disk by that point, and a
    REOPENED store (index rebuilt from those canonical files) shows the successor
    `accepted auto:auto-all` and the predecessor `superseded` -- a self-certified
    superseder and a half-applied deferred supersession, permanently committed.
    Mirrors `scratchpad/probe_t2_superseder.py`: a mixed cascade set (one eligible attached
    fact plus one unanchored injected fact, I4), a canonical-file byte comparison, and a
    reopened store."""
    reader = _feature_graph(tmp_path)
    [predecessor_result] = capture.propose(
        [_eligible_decision_draft(title="predecessor")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    predecessor_id = predecessor_result.decision_id
    assert store.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED

    injected: dict[str, str] = {}
    snapshots: dict[str, dict[str, str]] = {}
    real_ratify = Store.ratify

    def racing_ratify(self, decision_id, **kw):
        f = Fact(
            statement="unanchored, landed by a second connection mid-window",
            source="t2 probe",
            supports=[decision_id],
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
        self.add_fact(f)
        injected["id"] = f.id
        snapshots["before"] = _canonical_snapshot(self)
        try:
            return real_ratify(self, decision_id, **kw)
        finally:
            snapshots["after"] = _canonical_snapshot(self)

    monkeypatch.setattr(Store, "ratify", racing_ratify)

    [successor_result] = capture.propose(
        [
            _eligible_decision_draft(
                title="successor", supersedes=predecessor_id, facts=[_eligible_fact_draft()]
            )
        ],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )

    assert successor_result.status == "written"
    assert successor_result.ratified_by is None
    assert successor_result.auto_ratify_error is not None
    assert "cascade guard refused" in successor_result.auto_ratify_error

    assert snapshots["before"] == snapshots["after"], (
        "canonical files changed despite the guard's refusal"
    )

    predecessor = store.get_decision(predecessor_id)
    successor = store.get_decision(successor_result.decision_id)
    assert predecessor.status == DecisionStatus.ACCEPTED
    assert predecessor.valid_to is None
    assert successor.status == DecisionStatus.PROPOSED

    # A reopened store -- index rebuilt straight from canonical files whenever their digest
    # moved -- must agree. This is the check a `late`-guard mutant fails: it satisfies the
    # index-only assertions above (rolled back on the raised ValueError) while the
    # canonical files it already rewrote make a reopen show a fully self-certified
    # supersession.
    store.close()
    reopened = Store(tmp_path / "t.db")
    try:
        assert reopened.get_decision(predecessor_id).status == DecisionStatus.ACCEPTED
        assert reopened.get_decision(predecessor_id).valid_to is None
        assert reopened.get_decision(successor_result.decision_id).status == DecisionStatus.PROPOSED
        assert reopened.get_fact(injected["id"]).status == DecisionStatus.PROPOSED
    finally:
        reopened.close()


def test_cascade_race_guard_still_admits_an_eligible_injected_fact(store, tmp_path, monkeypatch):
    """Positive control for the regressions above: `cascade_guard` must be a genuine
    RE-CHECK, not a blanket refusal of every cascade -- a fact injected in the identical
    race shape, but that carries its OWN live anchor, must still let the decision (and this
    one injected fact) auto-ratify normally. Without this control, a guard hardcoded to
    always return `False` would also make the regression tests above pass for the wrong
    reason."""
    reader = _feature_graph(tmp_path)
    injected: dict[str, str] = {}
    real_ratify = Store.ratify

    def racing_ratify(self, decision_id, **kw):
        f = Fact(
            statement="landed by a concurrent writer mid-cascade, well-anchored this time",
            source="race regression control",
            supports=[decision_id],
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
        self.add_fact(f)
        resolve_and_bind(f.id, Descriptor(name="Trader", file_path="trader/exec.py"), reader, self)
        injected["id"] = f.id
        return real_ratify(self, decision_id, **kw)

    monkeypatch.setattr(Store, "ratify", racing_ratify)

    [result] = capture.propose(
        [_eligible_decision_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )

    assert result.status == "written"
    assert result.ratified_by == "auto:auto-low-risk"
    assert result.auto_ratify_error is None
    assert store.get_decision(result.decision_id).status == DecisionStatus.ACCEPTED

    injected_fact = store.get_fact(injected["id"])
    assert injected_fact.status == DecisionStatus.ACCEPTED
    assert injected_fact.ratified_by == "auto:auto-low-risk"


def test_domain_activation_failure_reports_and_batch_continues(store, tmp_path, monkeypatch):
    """A2 regression: `activate_accepted_domain`'s own refresh-failure handler
    (`sync.py`) writes `VOLATILE_STALE_KEY` unprotected -- when THAT write also raises,
    the exception used to escape `_propose_domain_one` unguarded (external review,
    reproduced by `scratchpad/probe_activation.py`): first domain accepted, second domain
    never processed, no batch result, no TOC rebuild -- a D6 violation ("the batch
    continues"). Ruling R wraps the call at the auto call site only (never inside
    `sync.activate_accepted_domain` itself, which the human MCP/CLI wrappers also use
    unchanged): the first domain KEEPS its stamp (the transition already committed),
    reports an `"activation: ..."` error, and the batch proceeds to the second domain."""
    import sidegraph.sync as sync_mod
    from sidegraph.store import VOLATILE_STALE_KEY

    reader = _feature_graph(tmp_path)
    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("refresh blew up")),
    )
    real_set_meta = Store.set_meta

    def failing_set_meta(self, key, value):
        if key == VOLATILE_STALE_KEY:
            raise RuntimeError("stale marker write failed")
        return real_set_meta(self, key, value)

    monkeypatch.setattr(Store, "set_meta", failing_set_meta)

    first, second = capture.propose_domains(
        [_eligible_domain_draft(slug="alpha-dom"), _eligible_domain_draft(slug="beta-dom")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )

    assert first.status == "proposed"
    assert first.ratified_by == "auto:auto-all"  # the transition itself already committed
    assert first.auto_ratify_error == "activation: stale marker write failed"
    assert store.get_domain(first.domain_id).status == DomainStatus.ACCEPTED

    assert second.status == "proposed"  # the batch did not abort after the first failure
    assert second.ratified_by == "auto:auto-all"
    assert second.auto_ratify_error == "activation: stale marker write failed"
    assert store.get_domain(second.domain_id).status == DomainStatus.ACCEPTED

    # the once-per-batch TOC rebuild still happens (design D2) despite both activations
    # having failed to resolve membership.
    toc = json.loads(store.get_meta(capture.TOC_CACHE_KEY))
    slugs = {d["slug"] for d in toc["domains"]}
    assert {"alpha-dom", "beta-dom"} <= slugs


def test_domain_activation_keyboard_interrupt_propagates(store, tmp_path, monkeypatch):
    """m1: the activation catch Ruling R added in `_propose_domain_one` must be
    `except Exception`, never `except BaseException` -- the same distinction
    `_auto_ratify` itself already makes, guarded there by
    `test_auto_ratify_lets_system_exit_and_keyboard_interrupt_propagate`. This site had no
    equivalent: a `KeyboardInterrupt` raised from `activate_accepted_domain` must still
    propagate out of `capture.propose_domains`, not be swallowed into `auto_ratify_error`."""
    reader = _feature_graph(tmp_path)

    def boom(domain, store_, reader_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(capture, "activate_accepted_domain", boom)

    with pytest.raises(KeyboardInterrupt):
        capture.propose_domains(
            [_eligible_domain_draft()], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL
        )


# ── Task 5: wire the batch pipelines through the same predicate/stamp (design D2
# pipelines 2/3 -- importer.import_rationales, doc_import.import_docs,
# domains.bootstrap_domains) ─────────────────────────────────────────────────────────
#
# Same predicate (`auto_ratify_eligible`), same stamp helper (`_auto_ratify`), same
# `_anchor_signal`/`_domain_anchored` gates Tasks 2-4 already proved -- these tests are
# wiring/adapter proofs (does each pipeline's own write result map TRUTHFULLY onto an
# `AutoEligibility`, and does its own post-write block call the right transition), never
# a second copy of the predicate's own logic tests. Cross-file fixture imports below
# reuse each pipeline's own committed test fixtures rather than inventing parallel ones
# (test-inventory.md "no two tasks invent the same builder" rule), same precedent as this
# module's own `from tests.test_store_ratification import _proposed` at the top (see the
# top-of-file `from tests.test_doc_import import ...` block for the Task 5 aliases).

# Two rationale nodes with distinguishable first text lines (which become their titles)
# and distinct `rationale_for` targets in distinct files -- the Q11 dedup-distinguishable
# shape every N-record importer fixture needs (a shared first line would collapse the
# second write into the first via `store.find_decision_by_title`, silently reducing N to
# 1 and making a "second record still processed" assertion vacuous).
_IMPORTER_GRAPH: dict = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "rat_a",
            "label": "Retries are idempotent to survive at-least-once delivery",
            "norm_label": "retries are idempotent to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "rat_b",
            "label": "Backoff caps retries to avoid a thundering herd",
            "norm_label": "backoff caps retries to avoid a thundering herd",
            "file_type": "rationale",
            "source_file": "retry.py",
            "community": 1,
        },
        {
            "id": "fn_retry",
            "label": "schedule_retry",
            "norm_label": "schedule_retry",
            "file_type": "code",
            "source_file": "retry.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat_a", "target": "fn_submit"},
        {"relation": "rationale_for", "source": "rat_b", "target": "fn_retry"},
    ],
}


def _importer_graph(tmp_path: Path, name: str = "importer_graph.json") -> GraphifyReader:
    path = tmp_path / name
    path.write_text(json.dumps(_IMPORTER_GRAPH))
    return GraphifyReader(path)


def _store_dir_snapshot(store: Store) -> dict[str, str]:
    """Whole-store-dir byte snapshot for a T16 "nothing was written" proof (dispatch
    context amendment 30 caveat): `_canonical_snapshot` above only hashes
    `decisions/`/`facts/`, which is vacuous for a domain-bootstrap dry run (a write would
    land under `domains/`) and says nothing about `bindings/`/`entities/` either. Every
    file under the store root, `index.db` included -- measured byte-identical across all
    three pipelines' dry runs at HEAD (pre-flight review Q16 item 9 probe)."""
    return {
        str(p.relative_to(store.path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(store.path.rglob("*"))
        if p.is_file()
    }


def _spy_all_transitions(monkeypatch) -> list[str]:
    """T16 (dispatch context amendment 30): spy every ratify-shaped transition PLUS the
    domain activation helper at once, class/module level, so a dry run's own report is
    never the only witness that nothing fired -- whichever pipeline is under test, all
    four names are watched."""
    import sidegraph.domains as domains_mod

    calls: list[str] = []
    for name in ("ratify", "ratify_fact", "ratify_domains"):
        real = getattr(Store, name)

        def _make_spy(real_fn, label):
            def _spy(self, *a, **k):
                calls.append(label)
                return real_fn(self, *a, **k)

            return _spy

        monkeypatch.setattr(Store, name, _make_spy(real, name))

    real_activate = domains_mod.activate_accepted_domain

    def _spy_activate(domain, store_, reader_):
        calls.append("activate_accepted_domain")
        return real_activate(domain, store_, reader_)

    monkeypatch.setattr(domains_mod, "activate_accepted_domain", _spy_activate)
    return calls


# -- importer.import_rationales -----------------------------------------------------


def test_importer_gotcha_auto_accepts_under_auto_low_risk_and_rerun_does_not_double_count(
    store, tmp_path
):
    """Step 1 (brief): an eligible importer `gotcha` (live Tier-2 anchor bound from its
    own `rationale_for` target, no supersedes) auto-accepts under `auto-low-risk`. A
    rerun hits the importer's own idempotency skip (title + provenance.ref match, before
    the write ever happens) rather than the auto-block again -- `auto_ratified` must stay
    at 0 on the skipped rerun, not double-count the same record."""
    reader = _importer_graph(tmp_path)
    report = import_rationales(
        store,
        reader,
        kind="gotcha",
        propose=True,
        limit=1,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert report.imported == 1
    assert report.auto_ratified == 1
    assert report.auto_ratify_failures == []
    decision = next(store.iter_decisions())
    assert decision.status == DecisionStatus.ACCEPTED
    assert decision.ratified_by == "auto:auto-low-risk"

    rerun = import_rationales(
        store,
        reader,
        kind="gotcha",
        propose=True,
        limit=1,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert rerun.imported == 0
    assert rerun.skipped_existing == 1
    assert rerun.auto_ratified == 0
    assert rerun.auto_ratify_failures == []
    assert len(list(store.iter_decisions())) == 1


def test_importer_propose_false_control_never_attempts_auto_ratify(store, tmp_path):
    """Amendment 21: `propose=False` lands `accepted` through the importer's own
    pre-existing flag, independent of `ratify_policy` -- the auto-block's own `propose`
    guard, not `auto_ratify_eligible`'s kind gate, is what keeps this record out (a
    dropped `propose` guard would instead attempt `Store.ratify` on an already-accepted
    record and report a spurious failure)."""
    reader = _importer_graph(tmp_path)
    report = import_rationales(
        store,
        reader,
        kind="gotcha",
        propose=False,
        limit=1,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert report.imported == 1
    decision = next(store.iter_decisions())
    assert decision.status == DecisionStatus.ACCEPTED
    assert decision.ratified_by is None
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []


def test_importer_default_adr_auto_accepts_only_under_auto_all(tmp_path):
    """T6 decision half (Q16 item 4: the brief names no pipeline; the importer's default
    `kind="adr"` is the natural carrier): eligible over every other gate, `adr` is
    admitted only by `auto-all`'s kind set, never `auto-low-risk` or `manual` -- the same
    otherwise-eligible draft, three policies, one store per policy so no run's
    idempotency skip masks another's."""
    reader = _importer_graph(tmp_path)
    for policy, expected_status, expected_stamp in (
        (RatifyPolicy.MANUAL, DecisionStatus.PROPOSED, None),
        (RatifyPolicy.AUTO_LOW_RISK, DecisionStatus.PROPOSED, None),
        (RatifyPolicy.AUTO_ALL, DecisionStatus.ACCEPTED, "auto:auto-all"),
    ):
        with Store(tmp_path / f"t-{policy.value}.db") as policy_store:
            report = import_rationales(
                policy_store, reader, propose=True, limit=1, ratify_policy=policy
            )
            assert report.imported == 1
            decision = next(policy_store.iter_decisions())
            assert decision.kind.value == "adr"
            assert decision.status == expected_status
            assert decision.ratified_by == expected_stamp


def test_importer_dry_run_never_auto_ratifies(store, tmp_path, monkeypatch):
    """T16 importer half. `not dry_run` is a DECLARED EQUIVALENT MUTANT here (Ruling B2,
    dispatch context): the importer's own `if dry_run: ... continue` (importer.py)
    returns before the write, so `_anchor_signal` is never even reachable -- see the
    task report's mutation-evidence table instead of a red target for that literal
    alone; "delete the dry-run `continue`" is the wiring mutation this test actually
    kills."""
    reader = _importer_graph(tmp_path)
    graph_bytes_before = reader.path.read_bytes()
    store_files_before = _store_dir_snapshot(store)
    calls = _spy_all_transitions(monkeypatch)

    report = import_rationales(
        store,
        reader,
        kind="gotcha",
        propose=True,
        dry_run=True,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    # A would-be WRITE, not proof of eligibility by itself (review round 1, m3) -- this
    # exact fixture (same graph, same `kind="gotcha"`) IS eligible, proven on the
    # non-dry-run path by
    # test_importer_gotcha_auto_accepts_under_auto_low_risk_and_rerun_does_not_double_count.
    assert report.imported >= 1
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []
    assert calls == []
    assert list(store.iter_decisions()) == []
    assert reader.path.read_bytes() == graph_bytes_before
    assert _store_dir_snapshot(store) == store_files_before


def test_importer_ratify_failure_reported_and_batch_continues(store, tmp_path, monkeypatch):
    """T15 importer: two rationale nodes with distinct first lines/anchors (Q11) so both
    survive the importer's own idempotency dedup -- `imported == 2` is asserted before
    either record's fate. Instance-patches `store.ratify` to fail on its first call
    (mirrors capture's own `test_ratify_exception_is_reported_and_batch_continues`,
    `tests/test_ratify_policy.py:1626`): the first decision stays proposed with its
    failure listed, the second still auto-accepts."""
    reader = _importer_graph(tmp_path)
    real_ratify = store.ratify
    calls = {"n": 0}

    def flaky_ratify(decision_id, *, actor=None, cascade_guard=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("not proposed")
        return real_ratify(decision_id, actor=actor, cascade_guard=cascade_guard)

    monkeypatch.setattr(store, "ratify", flaky_ratify)

    report = import_rationales(
        store, reader, kind="gotcha", propose=True, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
    )
    assert report.imported == 2

    decisions = {d.title: d for d in store.iter_decisions()}
    first = decisions["Retries are idempotent to survive at-least-once delivery"]
    second = decisions["Backoff caps retries to avoid a thundering herd"]
    assert first.status == DecisionStatus.PROPOSED
    assert first.ratified_by is None
    assert second.status == DecisionStatus.ACCEPTED
    assert second.ratified_by == "auto:auto-low-risk"
    assert report.auto_ratified == 1
    assert report.auto_ratify_failures == [f"{first.id}: not proposed"]


# -- doc_import.import_docs ----------------------------------------------------------


def test_doc_import_gotcha_auto_accepts_under_auto_low_risk(store, tmp_path):
    """Step 1 (brief): mirrors the importer's own low-risk eligibility test, over the
    doc-import pipeline's own write result -- a live Tier-2 mention anchor
    (`submit_order`), `--kind gotcha` override, `--propose`."""
    reader = _doc_reader(tmp_path, _DOC_IDENTIFIER_GRAPH)
    path = str(
        _doc_write_md(
            tmp_path,
            "docs/a.md",
            _doc_adr("Submit path", decision="Calls `submit_order` on success."),
        )
    )

    report = import_docs(
        store,
        reader,
        [path],
        kind="gotcha",
        propose=True,
        any_doc=True,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert report.imported == 1
    assert report.auto_ratified == 1
    assert report.auto_ratify_failures == []
    decision = next(store.iter_decisions())
    assert decision.kind.value == "gotcha"
    assert decision.status == DecisionStatus.ACCEPTED
    assert decision.ratified_by == "auto:auto-low-risk"


def test_doc_import_rejected_status_with_propose_never_reports_spurious_failure(store, tmp_path):
    """Amendment 22 / Ruling V (B1): a `status: rejected` doc imported with `--propose`
    lands REJECTED with `action == "written"` -- `lands_proposed` is False (the
    `status_rejected` short-circuit), so the auto-block's OWN gate, not `_auto_ratify`'s
    error catch, is what keeps a non-proposed record from ever being attempted. Red
    against the brief's/pre-rev-9-spec's literal `propose and not status_derived and ...`
    condition, which admits this doc and reports a spurious "is not proposed" failure
    (pre-flight review Q3 probe)."""
    reader = _doc_reader(tmp_path, _DOC_IDENTIFIER_GRAPH)
    path = str(
        _doc_write_md(tmp_path, "docs/a.md", _doc_adr_with_status("Submit path", "rejected"))
    )

    report = import_docs(
        store, reader, [path], propose=True, any_doc=True, ratify_policy=RatifyPolicy.AUTO_ALL
    )
    assert report.imported == 1
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []
    decision = next(store.iter_decisions())
    assert decision.status == DecisionStatus.REJECTED
    assert decision.ratified_by is None


def test_doc_import_plain_doc_without_propose_stays_accepted_unstamped(store, tmp_path):
    """Amendment 22: a plain doc (no frontmatter status, no `--propose`) lands `accepted`
    through doc-import's own pre-existing default -- `lands_proposed` is False, so the
    auto-block never even computes a signal for an already-accepted record."""
    reader = _doc_reader(tmp_path, _DOC_IDENTIFIER_GRAPH)
    path = str(
        _doc_write_md(
            tmp_path,
            "docs/a.md",
            _doc_adr("Submit path", decision="Calls `submit_order` on success."),
        )
    )

    report = import_docs(store, reader, [path], any_doc=True, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert report.imported == 1
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []
    decision = next(store.iter_decisions())
    assert decision.status == DecisionStatus.ACCEPTED
    assert decision.ratified_by is None


def test_doc_import_draft_frontmatter_stays_proposed_regardless_of_propose_flag(tmp_path):
    """T18 (Q16 item 3 / Q11): a draft-frontmatter doc's OWN status-derived path already
    lands it `proposed` -- passing `--propose` on top changes nothing, even under
    `auto-all`, because the auto-block's `not status_derived` gate excludes it either
    way. Importing the SAME doc twice into ONE store would hit doc-import's own
    content-match dedup on the second run (making the brief's literal "same doc without
    then with --propose" vacuous), so this uses two separate stores, one per flag
    combination, each proving `imported == 1` / `status_derived_proposed == 1` before
    checking the stamp."""
    reader = _doc_reader(tmp_path, _DOC_IDENTIFIER_GRAPH)
    for propose_flag in (False, True):
        with Store(tmp_path / f"t-{propose_flag}.db") as policy_store:
            path = str(
                _doc_write_md(
                    tmp_path,
                    f"docs/{propose_flag}.md",
                    _doc_adr_with_status("Submit path", "draft"),
                )
            )
            report = import_docs(
                policy_store,
                reader,
                [path],
                propose=propose_flag,
                any_doc=True,
                ratify_policy=RatifyPolicy.AUTO_ALL,
            )
            assert report.imported == 1
            assert report.status_derived_proposed == 1
            decision = next(policy_store.iter_decisions())
            assert decision.status == DecisionStatus.PROPOSED
            assert decision.ratified_by is None
            assert report.auto_ratified == 0
            assert report.auto_ratify_failures == []


def test_doc_import_superseded_write_stays_proposed_under_auto_all(store, tmp_path):
    """T18 superseded case: an edited doc re-imported over its ACCEPTED ancestor with
    `--propose` lands `action == "superseded"` (pre-flight review Q3: `pipeline_clean` is
    `action == "written"` only) -- the auto-block's own action gate, not any kind/anchor
    gate, is what keeps a superseding write out under `auto-all`, even though the new
    record is shape-eligible by kind/anchor (D2 defers the predecessor's close to a
    successful `Store.ratify`, never to an unratified auto write)."""
    reader = _doc_reader(tmp_path, _DOC_IDENTIFIER_GRAPH)
    md_path = _doc_write_md(
        tmp_path, "docs/a.md", _doc_adr("Submit path", decision="Calls `submit_order` on success.")
    )
    import_docs(store, reader, [str(md_path)], any_doc=True, ratify_policy=RatifyPolicy.AUTO_ALL)
    ancestor = next(store.iter_decisions())
    assert ancestor.status == DecisionStatus.ACCEPTED

    md_path.write_text(
        _doc_adr("Submit path", decision="Now retries `submit_order` up to 3 times.")
    )
    report = import_docs(
        store,
        reader,
        [str(md_path)],
        propose=True,
        any_doc=True,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert report.imported == 0
    assert report.superseded == 1
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []

    successor = next(d for d in store.iter_decisions() if d.id != ancestor.id)
    assert successor.status == DecisionStatus.PROPOSED
    assert successor.ratified_by is None
    ancestor_after = store.get_decision(ancestor.id)
    assert ancestor_after.status == DecisionStatus.ACCEPTED


def test_doc_import_dry_run_never_auto_ratifies(store, tmp_path, monkeypatch):
    """T16 doc-import half. Same declared-equivalent-mutant note as the importer's own
    T16 test: doc-import's dry-run `DocWriteResult` carries `decision_id=None`, so
    `not dry_run` is equivalent there too (Ruling B2)."""
    reader = _doc_reader(tmp_path, _DOC_IDENTIFIER_GRAPH)
    path = str(
        _doc_write_md(
            tmp_path,
            "docs/a.md",
            _doc_adr("Submit path", decision="Calls `submit_order` on success."),
        )
    )
    graph_bytes_before = reader.path.read_bytes()
    store_files_before = _store_dir_snapshot(store)
    calls = _spy_all_transitions(monkeypatch)

    report = import_docs(
        store,
        reader,
        [path],
        kind="gotcha",
        propose=True,
        dry_run=True,
        any_doc=True,
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    # A would-be WRITE, not proof of eligibility by itself (review round 1, m3) -- this
    # exact fixture (same graph, same doc, same `kind="gotcha"`) IS eligible, proven on
    # the non-dry-run path by test_doc_import_gotcha_auto_accepts_under_auto_low_risk.
    assert report.imported >= 1
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []
    assert calls == []
    assert list(store.iter_decisions()) == []
    assert reader.path.read_bytes() == graph_bytes_before
    assert _store_dir_snapshot(store) == store_files_before


def test_doc_import_ratify_failure_reported_and_batch_continues(store, tmp_path, monkeypatch):
    """T15 doc-import: two docs at different paths (Q11: different paths never collide in
    doc-import's own ref-keyed dedup), each with its own resolvable mention -- both
    survive, `imported == 2` asserted first. Instance-patches `store.ratify` to fail on
    its first call."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "fn1",
                "label": "fn_one",
                "norm_label": "fn_one",
                "file_type": "code",
                "source_file": "a.py",
                "community": 1,
            },
            {
                "id": "fn2",
                "label": "fn_two",
                "norm_label": "fn_two",
                "file_type": "code",
                "source_file": "b.py",
                "community": 1,
            },
        ],
        "links": [],
    }
    reader = _doc_reader(tmp_path, graph)
    doc_a = str(
        _doc_write_md(
            tmp_path, "docs/a.md", _doc_adr("Doc A", decision="Calls `fn_one` on success.")
        )
    )
    doc_b = str(
        _doc_write_md(
            tmp_path, "docs/b.md", _doc_adr("Doc B", decision="Calls `fn_two` on success.")
        )
    )

    real_ratify = store.ratify
    calls = {"n": 0}

    def flaky_ratify(decision_id, *, actor=None, cascade_guard=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("not proposed")
        return real_ratify(decision_id, actor=actor, cascade_guard=cascade_guard)

    monkeypatch.setattr(store, "ratify", flaky_ratify)

    report = import_docs(
        store,
        reader,
        [doc_a, doc_b],
        kind="gotcha",
        propose=True,
        any_doc=True,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    assert report.imported == 2

    decisions = {d.title: d for d in store.iter_decisions()}
    first, second = decisions["Doc A"], decisions["Doc B"]
    assert first.status == DecisionStatus.PROPOSED
    assert first.ratified_by is None
    assert second.status == DecisionStatus.ACCEPTED
    assert second.ratified_by == "auto:auto-low-risk"
    assert report.auto_ratified == 1
    assert report.auto_ratify_failures == [f"{first.id}: not proposed"]


# -- domains.bootstrap_domains ---------------------------------------------------------


def test_bootstrap_eligible_domain_auto_accepts_only_under_auto_all(tmp_path, monkeypatch):
    """T6 domain half + T14: `tests/test_domains.py`'s GRAPH+LABELS gives exactly one
    clean-prefix candidate (`alpha-domain`, `path_prefixes=["domainA"]`) and one `[]`-
    prefix candidate (`mainwidget`) -- eligible only under `auto-all`, and only the clean
    one. `BootstrapReport.auto_ratified` is checked against canonical
    `store.get_domain(...).status`, never the report alone. `activate_accepted_domain`
    is spied (wrapping the real function -- Q12's vacuity trap: `communities` is
    pre-written at PROPOSE time regardless of activation, so it is never proof of an
    activation call) to prove it fired for `alpha-domain` only; the TOC is rebuilt
    exactly once and contains `alpha-domain`; `graph.json` (read-only engine input) stays
    byte-identical."""
    import sidegraph.domains as domains_mod
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    reader = GraphifyReader(graph_path)
    graph_bytes_before = graph_path.read_bytes()

    activation_calls: list[str] = []
    real_activate = domains_mod.activate_accepted_domain

    def spying_activate(domain, store_, reader_):
        activation_calls.append(domain.slug)
        return real_activate(domain, store_, reader_)

    monkeypatch.setattr(domains_mod, "activate_accepted_domain", spying_activate)

    build_toc_calls: list[int] = []
    real_build_toc = domains_mod.build_toc

    def spying_build_toc(store_):
        build_toc_calls.append(1)
        return real_build_toc(store_)

    monkeypatch.setattr(domains_mod, "build_toc", spying_build_toc)

    with Store(tmp_path / "t.db") as bstore:
        report = bootstrap_domains(bstore, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
        assert report.proposed == 2
        assert report.auto_ratified == 1
        assert report.auto_ratify_failures == []

        alpha = bstore.find_domain_by_slug("alpha-domain")
        mainwidget = bstore.find_domain_by_slug("mainwidget")
        assert alpha.status == DomainStatus.ACCEPTED
        assert alpha.ratified_by == "auto:auto-all"
        assert mainwidget.status == DomainStatus.PROPOSED
        assert mainwidget.ratified_by is None

        assert activation_calls == ["alpha-domain"]
        assert build_toc_calls == [1]
        toc = json.loads(bstore.get_meta(domains_mod.TOC_CACHE_KEY))
        slugs = {d["slug"] for d in toc["domains"]}
        assert "alpha-domain" in slugs

    assert graph_path.read_bytes() == graph_bytes_before


def test_bootstrap_eligible_domain_stays_proposed_under_manual_and_auto_low_risk(tmp_path):
    """Negative half of the "only under auto-all" claim above: the same eligible
    candidate stays proposed, unstamped, under both `manual` and `auto-low-risk`
    (domains are never eligible under `auto-low-risk`, D3)."""
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    reader = GraphifyReader(graph_path)

    for policy in (RatifyPolicy.MANUAL, RatifyPolicy.AUTO_LOW_RISK):
        with Store(tmp_path / f"t-{policy.value}.db") as policy_store:
            report = bootstrap_domains(policy_store, reader, ratify_policy=policy)
            assert report.proposed == 2
            assert report.auto_ratified == 0
            assert report.auto_ratify_failures == []
            alpha = policy_store.find_domain_by_slug("alpha-domain")
            assert alpha.status == DomainStatus.PROPOSED
            assert alpha.ratified_by is None


def test_bootstrap_lint_warning_candidate_stays_proposed_under_auto_all(store, tmp_path):
    """Review round 1, m2: `bootstrap_domains`' own `prefix_warnings` -- computed once per
    candidate and reused by `_domain_anchored` rather than re-linted -- had no test where
    it is actually NON-empty for an otherwise-eligible (non-empty-prefix) candidate; the
    only existing negative fixture, `mainwidget`, is ineligible for the unrelated reason
    that its OWN derived prefix is `[]`. A REAL fixture, not a patched lint function
    (pre-flight review Q4): pre-accepts a sibling domain whose `seed_anchors` file falls
    under `alpha-domain`'s own derived prefix (`domainA`), tripping
    `_lint_domain_path_prefixes` check (b) (subsumes an ACCEPTED domain's seed anchor)."""
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    reader = GraphifyReader(graph_path)

    sibling = Domain(
        slug="existing-sibling",
        title="Existing Sibling",
        summary="Pre-existing, unrelated to the candidate below.",
        seed_anchors=[Descriptor(name="Alpha", file_path="domainA/alpha.py")],
        provenance=Provenance(source="agent"),
    )
    store.add_domain(sibling)
    store.ratify_domains(accept=[sibling.domain_id])
    assert store.get_domain(sibling.domain_id).status == DomainStatus.ACCEPTED

    report = bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert report.proposed == 2  # both candidates still proposed -- the sibling collides
    # with neither slug nor community, so it never becomes a skipped_existing itself.
    assert [w["slug"] for w in report.warnings] == ["alpha-domain"]

    alpha = store.find_domain_by_slug("alpha-domain")
    assert alpha.status == DomainStatus.PROPOSED
    assert alpha.ratified_by is None
    assert report.auto_ratified == 0
    assert report.auto_ratify_failures == []


def test_bootstrap_dry_run_never_auto_ratifies(tmp_path, monkeypatch):
    """T16 bootstrap half."""
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    reader = GraphifyReader(graph_path)
    graph_bytes_before = graph_path.read_bytes()

    with Store(tmp_path / "t.db") as bstore:
        store_files_before = _store_dir_snapshot(bstore)
        calls = _spy_all_transitions(monkeypatch)

        report = bootstrap_domains(
            bstore, reader, dry_run=True, ratify_policy=RatifyPolicy.AUTO_ALL
        )
        assert report.proposed == 2
        assert report.auto_ratified == 0
        assert report.auto_ratify_failures == []
        assert calls == []
        assert list(bstore.iter_domains()) == []
        assert _store_dir_snapshot(bstore) == store_files_before

    assert graph_path.read_bytes() == graph_bytes_before


def test_bootstrap_ratify_transition_failure_reported_and_batch_continues(
    store, tmp_path, monkeypatch
):
    """T15 bootstrap: `tests/test_cli_domains.py`'s GRAPH+LABELS gives two clean, eligible
    candidates (`alpha-domain`/`domainA`, `other0`/`domainB`) -- `proposed == 2` asserted
    first. Instance-patches `store.ratify_domains` to return an error outcome on its
    first call and delegate afterwards (mirrors `test_domain_transition_error_outcome_
    is_reported`, `tests/test_ratify_policy.py:1768`)."""
    from tests.test_cli_domains import GRAPH as _cli_domains_graph
    from tests.test_cli_domains import LABELS as _cli_domains_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_cli_domains_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_cli_domains_labels))
    reader = GraphifyReader(graph_path)

    real_ratify_domains = store.ratify_domains
    calls = {"n": 0}

    def flaky_ratify_domains(accept=None, drop=None, *, actor=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {(accept or [])[0]: "error: domain vanished mid-flight"}
        return real_ratify_domains(accept=accept, drop=drop, actor=actor)

    monkeypatch.setattr(store, "ratify_domains", flaky_ratify_domains)

    report = bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert report.proposed == 2

    alpha = store.find_domain_by_slug("alpha-domain")
    other = store.find_domain_by_slug("other0")
    assert alpha.status == DomainStatus.PROPOSED
    assert alpha.ratified_by is None
    assert other.status == DomainStatus.ACCEPTED
    assert other.ratified_by == "auto:auto-all"
    assert report.auto_ratified == 1
    assert report.auto_ratify_failures == [f"{alpha.domain_id}: error: domain vanished mid-flight"]


def test_bootstrap_activation_refresh_failure_reports_and_still_counts_accepted(
    store, tmp_path, monkeypatch
):
    """T15(d): reader present, but `sync.refresh_domain_communities_now` raises for every
    domain -- each domain's own transition still succeeds (accepted, stamped), the
    activation failure is reported as `"activation: <reason>"`, and the pre-existing heal
    flag is set. Two eligible domains (`tests/test_cli_domains.py` fixture) so the batch-
    continuation half is provable too."""
    import sidegraph.sync as sync_mod
    from sidegraph.store import VOLATILE_STALE_KEY
    from tests.test_cli_domains import GRAPH as _cli_domains_graph
    from tests.test_cli_domains import LABELS as _cli_domains_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_cli_domains_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_cli_domains_labels))
    reader = GraphifyReader(graph_path)
    store.set_meta(VOLATILE_STALE_KEY, "0")
    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("resolve blew up")),
    )

    report = bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert report.proposed == 2
    assert report.auto_ratified == 2

    alpha = store.find_domain_by_slug("alpha-domain")
    other = store.find_domain_by_slug("other0")
    for d in (alpha, other):
        assert d.status == DomainStatus.ACCEPTED
        assert d.ratified_by == "auto:auto-all"
    assert sorted(report.auto_ratify_failures) == sorted(
        [
            f"{alpha.domain_id}: activation: resolve blew up",
            f"{other.domain_id}: activation: resolve blew up",
        ]
    )
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"


def test_bootstrap_activation_failure_with_stale_flag_write_failure_still_counts_and_continues(
    store, tmp_path, monkeypatch
):
    """Amendment 29(b): the Ruling R guard in the A2 shape (mirrors
    `test_domain_activation_failure_reports_and_batch_continues`,
    `tests/test_ratify_policy.py:2619`) -- `sync.activate_accepted_domain`'s own
    refresh-failure handler writes `VOLATILE_STALE_KEY` unprotected; when THAT write also
    raises, only a `try/except` at the bootstrap auto call site keeps the exception from
    escaping and aborting the batch before the second domain is ever processed. This is
    the ONLY shape that kills a missing `try/except` -- a plain refresh-raises test alone
    does not, because `activate_accepted_domain` already catches the refresh error
    itself."""
    import sidegraph.domains as domains_mod
    import sidegraph.sync as sync_mod
    from sidegraph.store import VOLATILE_STALE_KEY
    from tests.test_cli_domains import GRAPH as _cli_domains_graph
    from tests.test_cli_domains import LABELS as _cli_domains_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_cli_domains_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_cli_domains_labels))
    reader = GraphifyReader(graph_path)

    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("refresh blew up")),
    )
    real_set_meta = Store.set_meta

    def failing_set_meta(self, key, value):
        if key == VOLATILE_STALE_KEY:
            raise RuntimeError("stale marker write failed")
        return real_set_meta(self, key, value)

    monkeypatch.setattr(Store, "set_meta", failing_set_meta)

    report = bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert report.proposed == 2  # the batch did not abort after the first activation failure

    alpha = store.find_domain_by_slug("alpha-domain")
    other = store.find_domain_by_slug("other0")
    for d in (alpha, other):
        assert d.status == DomainStatus.ACCEPTED
        assert d.ratified_by == "auto:auto-all"
    assert report.auto_ratified == 2
    assert sorted(report.auto_ratify_failures) == sorted(
        [
            f"{alpha.domain_id}: activation: stale marker write failed",
            f"{other.domain_id}: activation: stale marker write failed",
        ]
    )

    toc = json.loads(store.get_meta(domains_mod.TOC_CACHE_KEY))
    slugs = {d["slug"] for d in toc["domains"]}
    assert {"alpha-domain", "other0"} <= slugs


def test_bootstrap_activation_keyboard_interrupt_propagates(store, tmp_path, monkeypatch):
    """Mirrors `test_domain_activation_keyboard_interrupt_propagates` (capture's own
    Ruling R site): the bootstrap auto call site's `try/except` must be `except
    Exception`, never `except BaseException` -- a `KeyboardInterrupt` raised from
    `activate_accepted_domain` still propagates out of `bootstrap_domains`."""
    import sidegraph.domains as domains_mod
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    reader = GraphifyReader(graph_path)

    def boom(domain, store_, reader_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(domains_mod, "activate_accepted_domain", boom)

    with pytest.raises(KeyboardInterrupt):
        bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)


def test_bootstrap_never_derives_a_prefix_the_refresh_cap_would_reject(store, tmp_path):
    """I2 bootstrap half (Ruling HH), history: this test used to pin a REAL,
    bootstrap-constructible instance of an asymmetry between `domains._derive_path_
    prefixes`'s breadth guard and `sync._recompute_domain_communities`'s refresh-time
    claim cap -- the two used DIFFERENT numerators over the SAME 0.2 figure (fix round 1,
    Minor 3: the denominators were always identical -- all distinct communities in the
    graph). The
    derivation guard excluded the candidate's own community from its "other communities"
    count (`other = dir_breadth[top_dir] - {community_id}`), while the refresh-time cap
    counted the full matched set, origin community included. Ten communities total: "c1"
    (5 members, its own home dir "trader/") plus "c2"/"c3" (1 member each, also under
    "trader/") plus 7 single-member filler communities. Under the OLD guard, derive's
    `other = {"c2", "c3"}` = `2/10 = 0.20`, not over the cap, so "trader" WAS derived and
    the domain auto-accepted; the very next activation-time refresh computed the full
    matched set `{"c1", "c2", "c3"}` = `3/10 = 0.30 > 0.2` and rejected it as overbroad --
    a human would have seen "path rule too broad" about a rule bootstrap itself had just
    written.

    Option A (owner-approved 2026-09-14) tightened derive to the SAME claimed-set-over-
    total ratio refresh already uses, candidate's own community included. On this
    identical graph the claimed set is now `{"c1", "c2", "c3"}` = `3/10 = 0.30 > 0.2` at
    DERIVE time too, so "trader" is never derived for c1 in the first place -- the route
    this test used to exercise (a bootstrap-derived rule tripping the refresh cap) is now
    impossible by construction. c1 ends up with no `path_prefixes` and no `seed_anchors`,
    so it is never `_domain_anchored`: bootstrap does not even attempt to auto-ratify it
    (no accepted-but-unhealed transition, no `auto_ratify_failures` entry for it). The
    hand-authored half of I2 (`test_domain_overbroad_path_rule_reports_and_stays_accepted`
    above) is now the ONLY live coverage of the overbroad-report sentence under
    `auto-all` -- a human explicitly hand-authoring `path_prefixes=["trader"]` still hits
    the refresh-time cap exactly as before, since that cap is unconditional over
    `path_prefixes` of ANY provenance, bootstrap-derived or not."""
    nodes = (
        [
            {
                "id": f"c1n{i}",
                "label": f"C1N{i}",
                "norm_label": f"c1n{i}",
                "file_type": "code",
                "source_file": f"trader/c1n{i}.py",
                "community": "c1",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "c2n0",
                "label": "C2N0",
                "norm_label": "c2n0",
                "file_type": "code",
                "source_file": "trader/c2n0.py",
                "community": "c2",
            },
            {
                "id": "c3n0",
                "label": "C3N0",
                "norm_label": "c3n0",
                "file_type": "code",
                "source_file": "trader/c3n0.py",
                "community": "c3",
            },
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(7)
        ]
    )
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "v1", "nodes": nodes, "links": []}))
    reader = GraphifyReader(graph_path)

    report = bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL, min_members=5)

    d = next(dm for dm in store.iter_domains() if dm.communities == ["c1"])
    assert d.path_prefixes == []  # claimed {c1,c2,c3} = 3/10 = 0.30 > 0.2 at derive time too now
    assert d.status == DomainStatus.PROPOSED  # never _domain_anchored -> never even attempted
    assert not any(m.startswith(f"{d.domain_id}:") for m in report.auto_ratify_failures)


def test_bootstrap_prints_the_overbroad_path_rule_like_the_other_three_renderers(
    store, tmp_path, monkeypatch
):
    """Minor 1 (fix round 1, review): bootstrap's OWN copy of the "path rule too broad"
    sentence (`domains.py`, the `auto_ratify_failures` branch right after
    `activate_accepted_domain` returns) had ZERO test coverage -- the review broke the
    string in a scratchpad copy of `domains.py` and the full suite (2281 tests) stayed
    green. The other three renderers all have one: capture.py
    (`test_domain_overbroad_path_rule_reports_and_stays_accepted` above), cli.py
    (`test_cli_ratify_prints_the_overbroad_path_rule_like_mcp_does`,
    `tests/test_cli_ratify.py`), server.py (`tests/test_server_domains.py`).

    Patches `activate_accepted_domain` -- the same seam
    `test_bootstrap_activation_keyboard_interrupt_propagates` above patches -- to return a
    synthetic overbroad `DomainActivation`. Major 1's fix (the test right above this one)
    means the real cap-tripping graph shape Ruling HH originally built for this branch no
    longer reaches it, so a patched activation is now the only way left to exercise
    bootstrap's copy of the sentence at all -- the review's own recommendation once Major
    1 landed. Reuses the standard bootstrap fixture (`tests/test_domains.GRAPH`):
    community "10" (6 members, exclusively under "domainA/") derives
    `path_prefixes == ["domainA"]` normally, which is enough for `_domain_anchored` to let
    bootstrap attempt auto-ratify at all -- the synthetic activation result is what turns
    that attempt into the overbroad report, not any real cap arithmetic here."""
    import sidegraph.domains as domains_mod
    from sidegraph.sync import DomainActivation
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    reader = GraphifyReader(graph_path)

    def _fake_overbroad_activate(domain, store_, reader_):
        return DomainActivation(
            resolved=True,
            overbroad={"slug": domain.slug, "title": domain.title, "matched": 3, "total": 10},
            error=None,
        )

    monkeypatch.setattr(domains_mod, "activate_accepted_domain", _fake_overbroad_activate)

    report = bootstrap_domains(store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)

    d = next(dm for dm in store.iter_domains() if dm.communities == ["10"])
    assert d.path_prefixes == ["domainA"]
    assert d.status == DomainStatus.ACCEPTED  # D6: accepted-but-unhealed still commits
    [failure] = [m for m in report.auto_ratify_failures if m.startswith(f"{d.domain_id}:")]
    assert failure == (
        f"{d.domain_id}: activation: path rule too broad: 'domainA' match 3/10 "
        "communities — not applied; seed_anchors, if any, still applied"
    )


# -- Amendment 32: the two authoring paths pinned OUT of auto-ratification (compat
# guards -- structurally impossible to auto-ratify since neither accepts a
# `ratify_policy` at all, so these have no red target). -------------------------------


def test_mcp_add_domain_never_auto_ratifies_even_under_auto_all(store, monkeypatch):
    from sidegraph.server import _add_domain_impl

    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    result = _add_domain_impl(store, None, slug="manual-mcp-domain", title="Manual", summary="x")
    assert result["status"] == "proposed"
    domain = store.get_domain(result["domain_id"])
    assert domain.status == DomainStatus.PROPOSED
    assert domain.ratified_by is None


def test_cli_domains_add_never_auto_ratifies_even_under_auto_all(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "manual-cli-domain",
                "--title",
                "Manual",
                "--summary",
                "x",
            ]
        )
        == 0
    )
    added = Store(db).find_domain_by_slug("manual-cli-domain")
    assert added.status == DomainStatus.PROPOSED
    assert added.ratified_by is None


# -- CLI: `, auto-ratified N` result-line segment + stderr failures (design D6) --------


def test_cli_rationale_import_prints_auto_ratified_segment_and_dry_run_omits_it(
    tmp_path, capsys, monkeypatch
):
    from tests.test_cli_import import GRAPH as _rationale_graph

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_rationale_graph))
    db = tmp_path / "t.db"
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-low-risk")

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--kind", "lesson", "--propose"]) == 0
    )
    out = capsys.readouterr().out
    assert "imported 1 decision(s), auto-ratified 1 (skipped: 0 existing, 0 unanchorable)" in out

    d = next(Store(db).iter_decisions())
    assert d.status == DecisionStatus.ACCEPTED
    assert d.ratified_by == "auto:auto-low-risk"

    # Review round 1, m1: D6 says the segment is added to the non-dry-run line ONLY --
    # the rationale-import dry-run printer had no test of its own before this fix.
    db2 = tmp_path / "t2.db"
    assert (
        import_main(
            ["--db", str(db2), "--graph", str(graph), "--kind", "lesson", "--propose", "--dry-run"]
        )
        == 0
    )
    out2 = capsys.readouterr().out
    assert "auto-ratified" not in out2


def test_cli_doc_import_prints_auto_ratified_segment_and_dry_run_omits_it(
    tmp_path, capsys, monkeypatch
):
    from tests.test_cli_import import _DOC_MENTION_GRAPH, _adr_md
    from tests.test_cli_import import _write_md as _cli_write_md

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_DOC_MENTION_GRAPH))
    _cli_write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")

    db = tmp_path / "t.db"
    assert (
        import_main(
            [
                "--db",
                str(db),
                "--graph",
                str(graph),
                "--docs",
                str(tmp_path / "docs"),
                "--any-doc",
                "--propose",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert (
        "imported 1 decision(s), superseded 0, auto-ratified 1 (skipped: 0 existing, "
        "0 unanchorable, 0 not-decision-shaped, 0 unparseable, 0 superseded-frontmatter, "
        "0 outside-profile)" in out
    )
    d = next(Store(db).iter_decisions())
    assert d.status == DecisionStatus.ACCEPTED
    assert d.ratified_by == "auto:auto-all"

    # D6: the dry-run summary line never gets the segment.
    db2 = tmp_path / "t2.db"
    assert (
        import_main(
            [
                "--db",
                str(db2),
                "--graph",
                str(graph),
                "--docs",
                str(tmp_path / "docs"),
                "--any-doc",
                "--propose",
                "--dry-run",
            ]
        )
        == 0
    )
    out2 = capsys.readouterr().out
    assert "auto-ratified" not in out2


def test_cli_domain_bootstrap_prints_auto_ratified_segment_and_dry_run_omits_it(
    tmp_path, capsys, monkeypatch
):
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    db = tmp_path / "t.db"
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")

    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "proposed 2 domain(s), auto-ratified 1 (skipped: 0 existing)" in out

    alpha = Store(db).find_domain_by_slug("alpha-domain")
    assert alpha.status == DomainStatus.ACCEPTED
    assert alpha.ratified_by == "auto:auto-all"

    # Review round 1, m1: D6 says the segment is added to the non-dry-run line ONLY --
    # the bootstrap dry-run printer had no test of its own before this fix.
    db2 = tmp_path / "t2.db"
    assert domains_main(["bootstrap", "--db", str(db2), "--graph", str(graph), "--dry-run"]) == 0
    out2 = capsys.readouterr().out
    assert "auto-ratified" not in out2


def test_cli_rationale_import_auto_ratify_failure_prints_on_stderr(tmp_path, capsys, monkeypatch):
    """Amendment 31: a class-level patched transition prints the exact
    `auto-ratify failure: <id>: <reason>` line on stderr. Review round 1, m1: this test
    used to be the ONLY one of the three CLI surfaces exercising cli.py's stderr loop --
    see `test_cli_doc_import_auto_ratify_failure_prints_on_stderr` and
    `test_cli_bootstrap_auto_ratify_failure_prints_on_stderr` below for the other two
    (deleting either of THEIR loops used to survive the suite)."""
    from tests.test_cli_import import GRAPH as _rationale_graph

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_rationale_graph))
    db = tmp_path / "t.db"
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-low-risk")

    def failing_ratify(self, decision_id, *, actor=None, cascade_guard=None):
        raise ValueError("boom")

    monkeypatch.setattr(Store, "ratify", failing_ratify)

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--kind", "lesson", "--propose"]) == 0
    )
    err = capsys.readouterr().err

    d = next(Store(db).iter_decisions())
    assert f"auto-ratify failure: {d.id}: boom" in err


def test_cli_doc_import_auto_ratify_failure_prints_on_stderr(tmp_path, capsys, monkeypatch):
    """Review round 1, m1: `cli.py`'s doc-import stderr loop (`:801`) had no test of its
    own -- deleting it used to survive the suite."""
    from tests.test_cli_import import _DOC_MENTION_GRAPH, _adr_md
    from tests.test_cli_import import _write_md as _cli_write_md

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_DOC_MENTION_GRAPH))
    _cli_write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))
    db = tmp_path / "t.db"
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")

    def failing_ratify(self, decision_id, *, actor=None, cascade_guard=None):
        raise ValueError("boom")

    monkeypatch.setattr(Store, "ratify", failing_ratify)

    assert (
        import_main(
            [
                "--db",
                str(db),
                "--graph",
                str(graph),
                "--docs",
                str(tmp_path / "docs"),
                "--any-doc",
                "--propose",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err

    d = next(Store(db).iter_decisions())
    assert f"auto-ratify failure: {d.id}: boom" in err


def test_cli_bootstrap_auto_ratify_failure_prints_on_stderr(tmp_path, capsys, monkeypatch):
    """Review round 1, m1: `cli.py`'s bootstrap stderr loop (`:1097`) had no test of its
    own -- it uses a different loop variable name (`failure`, not `entry`) and sits after
    the warnings loop, so it is not a copy of the rationale-import one. Class-level
    patches `Store.ratify_domains` to fail on its first call and delegate afterwards
    (mirrors `test_domain_transition_error_outcome_is_reported`,
    `tests/test_ratify_policy.py:1768`), over the two-eligible-candidate
    `tests/test_cli_domains.py` fixture so the CLI's own `, auto-ratified N` segment is
    checked alongside the stderr line."""
    from tests.test_cli_domains import GRAPH as _cli_domains_graph
    from tests.test_cli_domains import LABELS as _cli_domains_labels

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_cli_domains_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_cli_domains_labels))
    db = tmp_path / "t.db"
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-all")

    real_ratify_domains = Store.ratify_domains
    calls = {"n": 0}

    def flaky_ratify_domains(self, accept=None, drop=None, *, actor=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {(accept or [])[0]: "error: domain vanished mid-flight"}
        return real_ratify_domains(self, accept=accept, drop=drop, actor=actor)

    monkeypatch.setattr(Store, "ratify_domains", flaky_ratify_domains)

    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    captured = capsys.readouterr()
    assert "proposed 2 domain(s), auto-ratified 1 (skipped: 0 existing)" in captured.out

    store = Store(db)
    failed = next(d for d in store.iter_domains() if d.status == DomainStatus.PROPOSED)
    succeeded = next(d for d in store.iter_domains() if d.status == DomainStatus.ACCEPTED)
    assert succeeded.ratified_by == "auto:auto-all"
    assert (
        f"auto-ratify failure: {failed.domain_id}: error: domain vanished mid-flight"
        in captured.err
    )


# -- T7: no status flip outside store.py (spec T7; static mechanism guards, Task 6) --------
#
# Three mechanical halves plus a wiring-site pin, per the pre-flight review's Q9 design and
# the dispatch context's binding rules: (a) the stamp is built in exactly ONE construction
# expression; (b) every `actor=` keyword call site lives inside that one helper -- landed
# red-first (amendment 23) by mutating a human ratify path and watching this test fail
# naming it, then restoring (see task-6-report.md for the transcript); (c) the helper's
# source never flips a status field or writes a canonical record directly; C-3 pins the six
# `status=DecisionStatus.ACCEPTED` construction sites by (file, enclosing function), not by
# line number, so a moved site is a visible diff instead of a silent pass.

_SRC = Path(__file__).resolve().parent.parent / "src" / "sidegraph"


def _grep_py(pattern: re.Pattern[str]) -> list[tuple[Path, int]]:
    """Every (path, 1-indexed lineno) whose line matches ``pattern``, scanning
    ``src/sidegraph/**/*.py`` as plain text -- no subprocess, no shelling out to grep
    (amendment 21), so the ``*.py`` filter excludes the vendored viz JS by construction."""
    hits: list[tuple[Path, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append((path, lineno))
    return hits


def _auto_ratify_line_span() -> range:
    """The inclusive line range ``capture._auto_ratify``'s source occupies, as a
    ``range`` -- used to check a construction/call site found by ``_grep_py`` falls
    inside it."""
    src_lines, start_line = inspect.getsourcelines(capture._auto_ratify)
    return range(start_line, start_line + len(src_lines))


def _fmt_hits(hits: list[tuple[Path, int]]) -> str:
    """Render ``_grep_py`` hits as ``path:line`` pairs relative to ``_SRC`` (m8) -- a
    readable assertion-failure message, not a raw ``(PosixPath(...), lineno)`` dump."""
    return ", ".join(f"{p.relative_to(_SRC)}:{ln}" for p, ln in hits)


def test_t7a_stamp_built_in_exactly_one_construction_expression():
    """T7(a): ``f"auto:{policy.value}"`` is built in exactly ONE place in
    ``src/sidegraph/`` -- ``capture._auto_ratify`` (``capture.py:317``). The regex
    matches the f-string SOURCE FORM (``f"auto:{`` / ``f'auto:{``), which also catches
    prose that quotes it verbatim (rev 8's docstring rule) -- this happened twice already,
    in ``store.py`` (Task 3) and ``capture.py`` (Task 4). New prose must describe the
    stamp as ``auto:<policy>``, never quote the f-string form (amendment 26)."""
    hits = _grep_py(re.compile(r"""f["']auto:\{"""))
    assert len(hits) == 1, f"expected exactly 1 stamp construction site, found: {_fmt_hits(hits)}"
    path, lineno = hits[0]
    assert path.name == "capture.py"
    assert lineno in _auto_ratify_line_span(), (
        f"stamp construction at {path}:{lineno} is outside _auto_ratify's source span"
    )


def test_t7b_actor_keyword_used_only_inside_auto_ratify():
    """T7(b): every ``actor=`` keyword call site in ``src/sidegraph/`` lives inside
    ``capture._auto_ratify`` -- no other path can pass an ``auto:``-prefixed actor.
    Baseline at HEAD (measured, amendment 2): 3 sites, all in ``_auto_ratify``
    (``capture.py:320``, ``:323``, ``:331``).

    Landed red-first (amendment 23): a human ratify call site was mutated to inject an
    ``actor=`` keyword; this test then failed, naming the injected site as a fourth
    ``actor=`` outside ``_auto_ratify``. The mutation was restored and verified clean
    (see task-6-report.md's "T7(b) red-first (amendment 23)" section for the
    transcript). This is the TEXTUAL half only -- it cannot see an ``actor`` passed
    through a ``**`` splat; ``test_t7b_ast_actor_keyword_or_splat_only_inside_auto_ratify``
    below is the AST half that sees splats too (rev 11, checkpoint-3 external review)."""
    hits = _grep_py(re.compile(r"\bactor="))
    assert len(hits) == 3, f"expected exactly 3 actor= call sites, found: {_fmt_hits(hits)}"
    span = _auto_ratify_line_span()
    outside = [
        (p, ln) for p, ln in hits if p.relative_to(_SRC) != Path("capture.py") or ln not in span
    ]
    assert not outside, f"actor= used outside _auto_ratify: {_fmt_hits(outside)}"


_T7B_AST_RATIFY_METHODS = {"ratify", "ratify_fact", "ratify_domains"}


def _t7b_ast_actor_or_splat_hits() -> list[tuple[Path, int, str]]:
    """AST half of T7(b) (rev 11, checkpoint-3 external review): every ``ast.Call`` in
    ``src/sidegraph/**/*.py`` whose callee is a ``.ratify``/``.ratify_fact``/
    ``.ratify_domains`` attribute access (any receiver) AND passes ``actor`` -- either as
    an explicit keyword, or through a ``**`` splat (an ``ast.keyword`` node with ``arg is
    None``). ``actor`` is keyword-only on all three transitions (pinned by
    ``test_actor_is_keyword_only_on_all_three_transitions``), so keyword-or-splat is the
    complete set of ways a caller can pass it. Unlike the textual ``actor=`` grep above,
    this also catches ``store.ratify(id_, **{"actor": "auto:" + "test"})`` -- the exact
    shape of the checkpoint-3 external-review mutant at the human CLI accept call
    (``cli.py:209``), which that grep cannot see. Each hit carries the callee's ``attr``
    (checkpoint-3 fix round 2, Minor 2) so the caller can check EACH of ``ratify`` /
    ``ratify_fact`` / ``ratify_domains`` appears, not just that the count is 3 -- a count
    alone cannot tell "one site lost ``actor=`` while an unrelated call inside the helper
    gained one" from the healthy state."""
    hits: list[tuple[Path, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in _T7B_AST_RATIFY_METHODS):
                continue
            if any(kw.arg == "actor" or kw.arg is None for kw in node.keywords):
                hits.append((path, node.lineno, func.attr))
    return hits


def test_t7b_ast_actor_keyword_or_splat_only_inside_auto_ratify():
    """T7(b) AST half (rev 11, checkpoint-3 external review): a textual ``actor=`` grep
    cannot see an ``actor`` smuggled in through a ``**`` splat -- exactly the shape that
    survived every existing T7/C-3 guard plus 152 passing tests
    (``store.ratify(id_, **{"actor": "auto:" + "test"})`` at the human CLI accept call,
    ``cli.py:209``). This test walks every call site with ``ast`` instead of grepping
    text, and must find EXACTLY the three sites inside ``capture._auto_ratify``
    (``capture.py:320``, ``:323``, ``:331``) -- matched by ``inspect.getsourcelines``
    span, same as the textual (b) test above -- AND that each of ``ratify`` /
    ``ratify_fact`` / ``ratify_domains`` is one of the three, exactly once (checkpoint-3
    fix round 2, Minor 2: a bare count of 3 cannot distinguish the healthy state from one
    site silently losing ``actor=`` while a different call gains one)."""
    hits = _t7b_ast_actor_or_splat_hits()
    span = _auto_ratify_line_span()
    pairs = [(p, ln) for p, ln, _attr in hits]
    outside = [
        (p, ln) for p, ln in pairs if p.relative_to(_SRC) != Path("capture.py") or ln not in span
    ]
    assert not outside, (
        f"actor passed to a ratify transition outside _auto_ratify: {_fmt_hits(outside)}"
    )
    assert len(hits) == 3, (
        f"expected exactly 3 actor-or-splat call sites, found: {_fmt_hits(pairs)}"
    )
    attrs = sorted(attr for _p, _ln, attr in hits)
    assert attrs == ["ratify", "ratify_domains", "ratify_fact"], (
        "expected ratify/ratify_fact/ratify_domains each exactly once inside "
        f"_auto_ratify, found: {attrs}"
    )


def _t7b_alias_and_getattr_hits() -> list[tuple[Path, int]]:
    """Checkpoint-3 fix round 2, Minor 1(a): ``_t7b_ast_actor_or_splat_hits`` only sees a
    DIRECT ``.ratify(...)``-shaped call -- ``_r = store.ratify; _r(id_, actor="auto:x")``
    and ``getattr(store, "ratify")(id_, actor=...)`` both evade it (confirmed by the
    checkpoint-3 review's probe), because neither call site's ``func`` is itself an
    ``ast.Attribute`` with the tell-tale name. Two independent patterns, unioned: (1)
    every ``ast.Attribute`` node with ``attr`` in ``_T7B_AST_RATIFY_METHODS`` that is NOT
    the ``func`` of an ``ast.Call`` -- a bare reference, almost always bound to a name for
    a later call (identity-tracked per file via ``id()``, since two different files can
    reuse the same address once the first file's tree is freed); (2) every ``ast.Call`` to
    the builtin ``getattr`` whose second positional argument is a string constant in
    ``_T7B_AST_RATIFY_METHODS``. Baseline at HEAD (measured): 0 -- neither pattern occurs
    in the codebase today; any hit is new. This is INTENTIONALLY not restricted to
    hits-that-also-pass-actor: an alias/``getattr`` reference to one of the three
    transitions is itself the evasion vector regardless of whether the actor keyword is
    visible at its definition site, since the call it feeds is invisible to this walk.
    Still not exhaustive (checkpoint-3 fix round 3, out-of-scope observation 2): an
    aliased ``builtins.getattr`` itself, a computed attribute name (e.g.
    ``getattr(store, "rat" + "ify")``), and ``operator.methodcaller("ratify")(store)``
    all still evade this walk -- but any of those forms used at an EXISTING transition
    caller would still fail the T7(d) behavioral pins below, which check the stored
    VALUE rather than how the call was shaped."""
    hits: list[tuple[Path, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        call_func_ids = {
            id(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _T7B_AST_RATIFY_METHODS
                and id(node) not in call_func_ids
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in _T7B_AST_RATIFY_METHODS
            ):
                hits.append((path, node.lineno))
    return hits


def test_t7b_ast_no_alias_or_getattr_evasion_outside_auto_ratify():
    """Checkpoint-3 fix round 2, Minor 1(a): closes the alias/``getattr`` evasion the
    review's probe confirmed against ``_t7b_ast_actor_or_splat_hits`` --
    ``_r = store.ratify`` followed by ``_r(id_, **{"actor": "auto:" + "test"})`` in place
    of the human CLI accept call passes that test silently. Landed red-first against
    exactly that mutant (scripted replace with a backup at ``cli.py:209``,
    ``PYTHONDONTWRITEBYTECODE=1``, restored and verified clean -- see
    checkpoint-3-fix-report.md's "Fix round 2" section for the transcript). Baseline at
    HEAD (measured, stated in ``_t7b_alias_and_getattr_hits``'s docstring): 0 hits
    anywhere, so every hit found here -- inside ``_auto_ratify`` or not -- is new; today
    ``_auto_ratify`` calls all three transitions directly, never via alias or
    ``getattr``."""
    hits = _t7b_alias_and_getattr_hits()
    span = _auto_ratify_line_span()
    outside = [
        (p, ln) for p, ln in hits if p.relative_to(_SRC) != Path("capture.py") or ln not in span
    ]
    assert not outside, (
        f"ratify/ratify_fact/ratify_domains referenced by alias or getattr outside "
        f"_auto_ratify: {_fmt_hits(outside)}"
    )
    assert not hits, f"expected 0 alias/getattr references anywhere, found: {_fmt_hits(hits)}"


_T7C_FORBIDDEN_TOKENS = (
    "DecisionStatus.ACCEPTED",
    "DomainStatus.ACCEPTED",
    ".status =",
    "_write_",
)


def _t7c_forbidden_tokens_present(src: str) -> list[str]:
    return [tok for tok in _T7C_FORBIDDEN_TOKENS if tok in src]


def test_t7c_auto_ratify_source_has_no_status_flip_or_direct_write():
    """T7(c): ``_auto_ratify``'s source contains none of the four forbidden tokens --
    proof it routes through exactly one of the three C-2 transitions (``Store.ratify`` /
    ``ratify_fact`` / ``ratify_domains``) rather than flipping a status field or writing
    a canonical record itself (the rejected design, spec T7)."""
    assert _t7c_forbidden_tokens_present(inspect.getsource(capture._auto_ratify)) == []


def test_t7c_checker_bites_on_a_synthetic_status_flip():
    """``_auto_ratify`` itself must never be mutated (dispatch context: "Do not edit
    _auto_ratify") -- this proves the SAME check used above is not vacuously
    always-green by running it against a synthetic source string that DOES contain a
    status flip (amendment 24)."""
    synthetic = "def _fake():\n    predecessor.status = DecisionStatus.SUPERSEDED\n"
    assert _t7c_forbidden_tokens_present(synthetic) == [".status ="]


_C3_EXPECTED_SITES = {
    ("server.py", "_add_decision_impl"),
    ("server.py", "_supersede_decision_impl"),
    ("server.py", "_add_fact_impl"),
    ("server.py", "_supersede_fact_impl"),
    ("capture.py", "_propose_fact_one"),
    ("capture.py", "_propose_one"),
}


def _enclosing_function(tree: ast.Module, lineno: int) -> str | None:
    """The innermost ``def``/``async def`` whose source span contains ``lineno``, via
    ``ast``'s ``end_lineno`` -- never by line-number proximity, so a moved or reordered
    site is caught rather than silently misattributed."""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end and (best is None or node.lineno > best.lineno):
                best = node
    return best.name if best else None


def test_c3_accepted_construction_sites_pinned_by_file_and_function():
    """C-3: every ``status=DecisionStatus.ACCEPTED`` construction site in
    ``src/sidegraph/``, pinned by ``(file, enclosing function)`` via ``ast`` rather than
    line number -- a moved site shows up as a visible diff instead of a silent pass
    (amendment 25). Count stays 6 (unchanged by this feature, per the design)."""
    hits = _grep_py(re.compile(r"status=DecisionStatus\.ACCEPTED"))
    assert len(hits) == 6, f"expected exactly 6 sites, found: {_fmt_hits(hits)}"
    trees: dict[Path, ast.Module] = {}
    found: set[tuple[str, str]] = set()
    for path, lineno in hits:
        tree = trees.setdefault(
            path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        enclosing = _enclosing_function(tree, lineno)
        assert enclosing is not None, f"{path}:{lineno} has no enclosing function"
        found.add((path.name, enclosing))
    assert found == _C3_EXPECTED_SITES


# -- C1/C2: two more static zeros for the T7 guard family (post-merge follow-up, item C) --
# T7(c) above inspects ONLY `_auto_ratify`'s own source; nothing statically stopped code
# ELSEWHERE from flipping a record's status or writing a canonical record directly, bypassing
# the three `Store.ratify*` transitions (C1), nor stopped code elsewhere from computing a
# ratifier identity outside those same three transitions (C2). Both are guards, not evidence:
# GREEN against today's correct code, measured zero on the tree at HEAD, and can never go red
# against it -- each has a companion test proving the same collector is not vacuous, run over
# a synthetic source string containing the violation (never a mutation of a real source file).
#
# Fix round 1 (review Minor 4) widened both past their original narrow shapes -- the house
# standard set by `_t7b_alias_and_getattr_hits` above: close the cheap evasions AND declare
# what stays open. Every widening below was independently re-measured at 0 hits on the real
# tree (`src/sidegraph/`) before landing; see wave-1-report.md's "Fix round 1" section for the
# counts.
#
# Fix round 3 (post-merge follow-up, item C's own two follow-ups) closes C1's two
# reflective/string-keyed shapes the review PROVED produce an accepted canonical record with
# no ratify and no stamp (`object.__setattr__(x, "status", ...)`/`x.__setattr__("status", ...)`,
# `x.__dict__["status"] = ...`, and `getattr(x, "_write_...")`/`getattr(x, "_atomic_write...")`
# -- item 2b) and adds a `(file, function)` site pin, the C-3 idiom, for the THREE dataflow
# sites C1's own enum-mention/string-constant check cannot close (item 2a) -- see
# `_c1_hits_in_source`'s docstring for what is now caught, what is measured still open, and the
# re-run reproduction of the reviewer's mutant.

_C1_STATUS_ENUMS = {"DecisionStatus", "DomainStatus"}
# The record-status vocabulary a bare string constant can spell out (Minor 4): the union of
# both enums' values, not just DecisionStatus's -- a domain's "dropped" is a real status too.
_C1_STATUS_VALUES = {s.value for s in DecisionStatus} | {s.value for s in DomainStatus}
# `_write_*` (the canonical-writer naming convention) and `_atomic_write*` (the primitive
# every one of them ultimately calls, e.g. `_atomic_write_json`, `_atomic_write_text*`) --
# Minor 4's "the primitive every canonical writer calls, and it is module-level and
# importable" finding.
_C1_WRITER_PREFIXES = ("_write_", "_atomic_write")


def _flatten_targets(targets: list[ast.expr]) -> list[ast.expr]:
    """Descend into ``ast.Tuple``/``ast.List`` targets so ``d.status, x = ...`` is seen as
    the two leaf targets ``d.status`` and ``x``, not as one opaque ``ast.Tuple`` node
    (fix round 2, NEW Minor B -- a tuple target was an undeclared gap)."""
    flat: list[ast.expr] = []
    for t in targets:
        if isinstance(t, (ast.Tuple, ast.List)):
            flat.extend(_flatten_targets(t.elts))
        else:
            flat.append(t)
    return flat


def _mentions_status_enum_anywhere(value: ast.expr) -> bool:
    """True iff ANY node in ``value``'s subtree is a bare ``ast.Name`` or an
    ``ast.Attribute`` naming ``DecisionStatus``/``DomainStatus`` -- a full subtree walk
    (fix round 2, NEW Minor B), not the round-1 version's single linear ``ast.Attribute``
    chain. The chain-only version missed a value that mentions the enum anywhere BUT at
    its own top level: ``DecisionStatus.ACCEPTED if ok else DecisionStatus.REJECTED`` (an
    ``ast.IfExp``, the realistic shape -- one transition that either accepts or rejects),
    ``DecisionStatus("accepted")`` and ``getattr(DecisionStatus, name)`` (both
    ``ast.Call``, with ``DecisionStatus`` as an argument, not a chain root). All three are
    now caught the same way as the original bare/`.value`/module-qualified forms: the enum
    name appears somewhere in the subtree, full stop."""
    return any(
        (isinstance(node, ast.Name) and node.id in _C1_STATUS_ENUMS)
        or (isinstance(node, ast.Attribute) and node.attr in _C1_STATUS_ENUMS)
        for node in ast.walk(value)
    )


def _status_flip_value_caught(value: ast.expr) -> bool:
    """True iff ``value`` is one ``_c1_hits_in_source``'s Attribute-target branch already
    flags: it mentions ``DecisionStatus``/``DomainStatus`` anywhere in its subtree
    (``_mentions_status_enum_anywhere``), or is a bare string constant spelling a record
    status (``_C1_STATUS_VALUES``). Factored out (fix round 3, item 2a) so
    ``_c1_dataflow_status_assignment_sites`` below -- the site pin for the DATAFLOW gap
    this same check cannot close -- shares the IDENTICAL "is this value caught" test
    rather than re-deriving it, the same de-duplication discipline C-3's
    ``_enclosing_function`` already sets for this file."""
    return _mentions_status_enum_anywhere(value) or (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value in _C1_STATUS_VALUES
    )


def _c1_hits_in_source(src: str, filename: str = "<synthetic>") -> list[tuple[int, str]]:
    """Collect C1's forbidden shapes within one raw source string (parsed here, not
    pre-parsed by the caller) -- factored out so the real driver
    (``_c1_status_flip_or_canonical_write_hits`` below, which adds the file walk and the
    ``store.py`` exclusion) and the synthetic-mutation companion test share the IDENTICAL
    collector.

    Status-flip shape: an ``ast.Assign`` OR ``ast.AnnAssign`` with at least one
    ``ast.Attribute`` TARGET, once tuple/list targets are flattened (``_flatten_targets``
    -- ``d.status, x = ...`` counts; never a bare NAME target: ``doc_import.py:1782``'s
    ``status = DecisionStatus.REJECTED`` is a local, later passed as an argument --
    legitimate, and a NAME-target match would be red on arrival) whose value either
    mentions ``DecisionStatus``/``DomainStatus`` ANYWHERE in its subtree
    (``_mentions_status_enum_anywhere`` above -- covers a conditional, an enum call, and a
    ``getattr`` on the enum, not just an attribute chain) or is a bare string constant
    spelling a record status (``_C1_STATUS_VALUES`` -- ``d.status = "accepted"`` bypasses
    the enum silently, since ``schema.py``'s models have no ``validate_assignment``); a
    ``setattr(_, "status", _)`` call, the same flip through the builtin instead of
    attribute syntax; or a ``.model_copy(update={"status": ...})`` call with a literal
    ``"status"`` key -- syntactically as reachable as ``setattr`` (fix round 2, NEW Minor
    B: round 1's docstring wrongly called this one unreachable).

    Canonical-write shape: ANY ``ast.Name`` or ``ast.Attribute`` node (not only a call's
    ``func`` -- fix round 2, NEW Minor B, mirrors C2's own "any reference" widening below)
    whose name starts ``_write_``/``_atomic_write`` (``_C1_WRITER_PREFIXES``) -- covers a
    direct call (``store._write_decision_canonical(d)``, ``_atomic_write_json(path,
    obj)``), a writer bound to a local (``w = store._write_decision_canonical``), and one
    passed to ``functools.partial`` unevaluated; and an ``ast.ImportFrom`` pulling a
    ``_write_*``/``_atomic_write*`` name out of a module named ``store`` (relative or
    absolute), the import itself being the evasion vector regardless of whether the
    imported name is ever referenced again (mirrors ``_t7b_alias_and_getattr_hits``'s own
    reasoning for aliases/``getattr`` above).

    Still not exhaustive. This paragraph has now asserted a false "X evades" or "X is
    beyond reach" claim in each of two prior rounds (fix round 1: ``getattr(DecisionStatus,
    "...")`` does NOT evade -- ``_mentions_status_enum_anywhere``'s full-subtree walk
    catches it, confirmed by the companion test and re-run here; fix round 2: a WRITER
    import under an alias, e.g. ``from .store import _atomic_write_json as aw``, does NOT
    evade either -- the ``ast.ImportFrom`` check reads ``alias.name``, the ORIGINAL name,
    never ``asname``). Ruling (fix round 2, upheld through fix round 3): stop asserting
    what is beyond this collector's reach and state only what has just been measured, by
    re-running ``_c1_hits_in_source`` directly rather than trusting a prior round's
    docstring:

    - **Caught, confirmed by re-running just now:** a direct writer import under an alias
      through the ``store`` module (``from .store import _write_decision_canonical as
      w``); ``getattr(DecisionStatus, name)`` / ``getattr(DecisionStatus, "...")``
      (the enum name is a bare ``ast.Name`` argument, caught the same way a bare
      attribute chain is); ``object.__setattr__(x, "status", ...)``,
      ``x.__setattr__("status", ...)``, and any OTHER class-qualified unbound form
      (``Decision.__setattr__(x, "status", ...)``, ``type(x).__setattr__(x, "status",
      ...)``) (fix round 3, item 2b -- the review's own mutant shape, widened fix round 1
      review Minor 2: an ``ast.Call`` whose callee attr is ``__setattr__`` is a hit when a
      ``"status"`` constant sits at EITHER ``args[0]`` (the bound form's field position)
      or ``args[1]`` (every unbound form's field position, whatever the receiver spells),
      not gated on the receiver being literally the name ``object`` -- that gate missed
      every other class-qualified unbound spelling); ``x.__dict__["status"] = ...`` (fix
      round 3, item 2b: an assignment target
      that is an ``ast.Subscript`` on a ``__dict__`` attribute with the constant key
      ``"status"``, flagged on the target shape alone, the same convention
      ``setattr(_, "status", _)`` above already uses -- no condition on the value);
      ``getattr(x, "_write_...")``/``getattr(x, "_atomic_write...")`` (fix round 3, item
      2b -- the review's own mutant shape: a ``getattr`` call whose second argument is a
      string constant starting with a writer prefix).
    - **Open, confirmed by re-running just now:** ``getattr(schema,
      "DecisionStatus").ACCEPTED`` (the enum's name is a STRING argument to ``getattr``
      on the MODULE, no ``ast.Name``/``Attribute`` spells ``DecisionStatus`` itself);
      ``getattr(store, "_write_" + "fact_canonical")`` (a writer name built by
      concatenation rather than one literal ``ast.Constant`` string); an aliased ENUM
      import (``from .schema import DecisionStatus as DS``, then ``d.status =
      DS.ACCEPTED``); a writer re-exported through a module NOT named ``store`` and
      imported under an alias that does not itself start with a writer prefix; raw
      ``Path.write_text``/``store._conn.execute("UPDATE ...")`` writes.

    **Dataflow (fix round 3, item 2a):** outside ``store.py``, exactly THREE ``.status``
    assignments have a value ``_status_flip_value_caught`` does not flag as an enum
    mention or a record-status string -- ``anchoring.py:61`` (``AnchorResolution.__init__``,
    ``self.status = status``), ``sync.py:72`` (``_set_leaf_status``, ``b.status =
    status``), and ``sync.py:110`` (``_repoint_communities``, ``tb.status =
    "orphaned"``) -- all three legitimate ``AnchorBinding``/``AnchorResolution``
    transitions, none a ``Decision``/``Domain`` status flip; re-derived by an AST walk
    this session (not copied from a prior round's docstring), landing on the same count
    fix round 2 first measured. Fix round 2 recorded a ``(file, function)`` site pin for
    this gap -- the C-3 idiom -- as an unbuilt follow-up rather than build it there,
    citing this guard's then-approved scope of "two static zeros". Fix round 3 builds it:
    ``test_c1_dataflow_status_sites_pinned_by_file_and_function`` below pins exactly
    these three sites via ``_c1_dataflow_status_assignment_hits``, so a fourth ``.status``
    ADDITIONAL ``.status`` assignment landing in this same gap anywhere outside
    ``store.py`` is a visible diff in that pinned Counter (a set, which this pin used
    before its own review round, silently swallowed both duplicates and cross-subpackage
    basename collisions) instead of a silent pass. Replacing a pinned assignment in place
    keeps the count and stays green -- see that test's own docstring.

    **What the behavioral ``test_t7d_*`` pins (T7(d)) actually cover, measured, not
    assumed (fix round 3 correction carried forward: "cover the human-vs-auto boundary
    for every call site this codebase has TODAY" was false and demonstrably so):** they
    check ``ratified_by`` on the EIGHT human ratify doors (CLI and MCP accept for each of
    decision/fact/domain, plus the legacy MCP batch path and doc-import ratify) -- proving
    a human accept never stores an ``auto:`` stamp. They do NOT cover every call site.

    Re-measured this fix round, in a scratchpad copy (``PYTHONDONTWRITEBYTECODE=1``,
    ``sidegraph.__file__`` confirmed to resolve inside the scratchpad, never the
    checkout): the reviewer's own mutant -- ``object.__setattr__(fact, "status",
    DecisionStatus.ACCEPTED)`` then ``getattr(store, "_write_fact_canonical")(fact)``,
    inserted right after ``capture._propose_fact_one``'s ``store.add_fact(fact)`` -- run
    through a real ``propose_facts`` call BEFORE this fix round's two shapes were closed:
    the canonical file lands ``accepted`` with no ratifier stamp while the index (and a
    freshly reopened store) still reads ``proposed``, and this collector's own tests,
    T7(d), and the rest of the suite all stay green; only ``sidegraph-doctor``'s
    ``unratified-accept`` check catches it (1 finding on the mutant, 0 on clean code,
    once the store holds one genuine ratifier stamp to compare against -- that check's
    own scoping rule). AFTER closing the two shapes: the identical mutant now trips
    ``test_c1_no_status_flip_or_canonical_write_outside_store_py`` directly -- both the
    ``object.__setattr__`` line and the ``getattr`` writer call are reported as hits in
    ``capture.py``, so the guard fails before doctor is ever consulted. Doctor's check
    remains the runtime backstop for a value that reaches ``accepted`` through a shape no
    static collector here has been written for yet."""
    tree = ast.parse(src, filename=filename)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            flat_targets = _flatten_targets(targets)
            if any(
                isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Attribute)
                and t.value.attr == "__dict__"
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "status"
                for t in flat_targets
            ):
                # x.__dict__["status"] = ... (fix round 3, item 2b): flagged on the
                # target shape alone -- the key is a literal "status", unambiguous the
                # same way setattr(_, "status", _)'s field argument is below, so no
                # condition is layered on the value.
                hits.append((node.lineno, "status-flip"))
            has_attr_target = any(isinstance(t, ast.Attribute) for t in flat_targets)
            if has_attr_target and _status_flip_value_caught(node.value):
                hits.append((node.lineno, "status-flip"))
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "status"
            ):
                hits.append((node.lineno, "status-flip"))
            elif isinstance(func, ast.Attribute) and func.attr == "__setattr__" and node.args:
                # x.__setattr__("status", ...) [bound form: field at args[0]] and ANY
                # unbound form -- object.__setattr__(x, "status", ...),
                # Decision.__setattr__(x, "status", ...), type(x).__setattr__(x, "status",
                # ...) [field at args[1]] alike (fix round 3, item 2b; widened fix round 1
                # review Minor 2: gating args[1] on the receiver being literally the name
                # `object` missed every OTHER class-qualified unbound spelling).
                #
                # The position follows the ARITY, not the receiver (fix round 1 re-review
                # Minor B): `__setattr__` takes (name, value) bound and (obj, name, value)
                # unbound, so the field is args[0] at two arguments and args[1] at three.
                # Accepting "status" at either position regardless of arity flagged
                # `w.__setattr__("label", "status")` -- setting some other field TO the
                # string "status" -- as a status flip. Measured on the real tree: zero
                # `__setattr__` calls outside store.py at all, so both rules are 0 hits
                # there; this one is 0 hits for the right reason.
                field = (
                    node.args[0]
                    if len(node.args) == 2
                    else node.args[1]
                    if len(node.args) == 3
                    else None
                )
                if isinstance(field, ast.Constant) and field.value == "status":
                    hits.append((node.lineno, "status-flip"))
            elif isinstance(func, ast.Attribute) and func.attr == "model_copy":
                for kw in node.keywords:
                    if (
                        kw.arg == "update"
                        and isinstance(kw.value, ast.Dict)
                        and any(
                            isinstance(k, ast.Constant) and k.value == "status"
                            for k in kw.value.keys
                        )
                    ):
                        hits.append((node.lineno, "status-flip"))
                        break
            elif (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value.startswith(_C1_WRITER_PREFIXES)
            ):
                # getattr(x, "_write_...")/getattr(x, "_atomic_write...") (fix round 3,
                # item 2b): the writer reached through a string constant -- the literal
                # form the review's mutant used. A concatenated (non-Constant) name still
                # evades this check; see this function's own docstring.
                hits.append((node.lineno, "canonical-write-getattr"))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            # ANY reference, not only a call's `func` (NEW Minor B): closes a writer bound
            # to a local, or passed unevaluated to `functools.partial`, the same class C2's
            # widening already closes for `_ratifier_identity` below.
            writer_name = node.id if isinstance(node, ast.Name) else node.attr
            if writer_name.startswith(_C1_WRITER_PREFIXES):
                hits.append((node.lineno, "canonical-write"))
        elif isinstance(node, ast.ImportFrom):
            module_tail = (node.module or "").rsplit(".", 1)[-1]
            if module_tail == "store" and any(
                alias.name.startswith(_C1_WRITER_PREFIXES) for alias in node.names
            ):
                hits.append((node.lineno, "canonical-write-import"))
    return hits


def _c1_status_flip_or_canonical_write_hits() -> list[tuple[Path, int, str]]:
    """C1's real driver: ``_c1_hits_in_source`` over every file in ``src/sidegraph/``
    EXCLUDING ``store.py`` -- the only module allowed to flip a record's status or call a
    canonical ``_write_*``/``_atomic_write*`` writer. Deliberately AST, not a textual
    ``.status =`` grep: that would also flag ``AnchorBinding``'s own string ``status`` field
    assignments (``sync.py:72``, ``:110``, ``anchoring.py:61``) -- a different field
    entirely, not a record-status flip. Excludes by PATH, not basename (Minor 5, mirrors
    T7(b)'s own form above): a future ``src/sidegraph/<pkg>/store.py`` must still be
    scanned, not silently skipped whole."""
    hits: list[tuple[Path, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.relative_to(_SRC) == Path("store.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, shape in _c1_hits_in_source(text, filename=str(path)):
            hits.append((path, lineno, shape))
    return hits


def test_c1_no_status_flip_or_canonical_write_outside_store_py():
    """C1: nothing outside ``store.py`` flips a record's status field or calls a canonical
    ``_write_*``/``_atomic_write*`` writer directly -- T7(c) above pins only
    ``_auto_ratify``'s own source; this pins everywhere else, closing the gap external
    review noted. Exactly zero today (measured zero on the tree at HEAD) -- this is a GUARD,
    not evidence: it is green against today's correct code and can never go red against it.
    ``test_c1_checker_bites_on_a_synthetic_status_flip_and_write`` below proves the same
    collector is not vacuously always-empty."""
    hits = _c1_status_flip_or_canonical_write_hits()
    rendered = ", ".join(f"{p.relative_to(_SRC)}:{ln} ({shape})" for p, ln, shape in hits)
    assert not hits, (
        f"status flip or canonical write outside store.py -- route it through "
        f"Store.ratify/ratify_fact/ratify_domains (or a canonical _write_*/_atomic_write* "
        f"call) inside store.py instead: {rendered}"
    )


def test_c1_checker_bites_on_a_synthetic_status_flip_and_write():
    """Proves ``_c1_hits_in_source`` -- the SAME collector ``test_c1_...`` above runs over
    real source -- is not vacuously always-empty: run it over a synthetic source string
    exercising every shape (round 1's original two, fix round 2's widenings, and fix
    round 3's item-2b closures -- ``object.__setattr__``/``__setattr__``,
    ``__dict__["status"]``, and the ``getattr`` writer reach) and assert each is
    reported, by shape label, the exact number of times expected. A ``Counter`` rather
    than an ordered-list equality, since ``ast.walk`` is breadth-first (a top-level
    ``ast.ImportFrom`` would sort before same-function statements it textually follows)
    -- the count per shape is what this test is proving, not traversal order. Not a
    mutation of a real source file (item C's brief)."""
    synthetic = (
        "def _fake(d, ok, name, store, path, obj):\n"
        "    from .store import _write_decision_canonical\n"  # canonical-write-import
        "    d.status = DecisionStatus.ACCEPTED\n"  # status-flip: bare enum
        "    d.status = DecisionStatus.ACCEPTED.value\n"  # status-flip: .value accessor
        "    d.status = schema.DecisionStatus.ACCEPTED\n"  # status-flip: module-qualified
        "    d.status: DecisionStatus = DecisionStatus.ACCEPTED\n"  # status-flip: AnnAssign
        '    d.status = "accepted"\n'  # status-flip: string constant
        '    setattr(d, "status", "accepted")\n'  # status-flip: setattr
        "    d.status = DecisionStatus.ACCEPTED if ok else DecisionStatus.REJECTED\n"  # conditional
        '    d.status = DecisionStatus("accepted")\n'  # status-flip: enum call
        "    d.status = getattr(DecisionStatus, name)\n"  # status-flip: getattr on enum
        "    d.status, x = DecisionStatus.ACCEPTED, 1\n"  # status-flip: tuple target
        '    d2 = d.model_copy(update={"status": "accepted"})\n'  # status-flip: model_copy
        '    object.__setattr__(d, "status", "accepted")\n'  # status-flip: unbound __setattr__
        '    d.__setattr__("status", "accepted")\n'  # status-flip: bound __setattr__
        '    d.__dict__["status"] = "accepted"\n'  # status-flip: __dict__ subscript
        "    store._write_decision_canonical(d)\n"  # canonical-write: attribute call
        "    _atomic_write_json(path, obj)\n"  # canonical-write: bare-name call
        "    w = store._write_decision_canonical\n"  # canonical-write: bound reference
        '    getattr(store, "_write_decision_canonical")(d)\n'  # getattr-writer: _write_
        '    getattr(store, "_atomic_write_json")(path, obj)\n'  # getattr-writer: _atomic_write
    )
    hits = _c1_hits_in_source(synthetic)
    assert Counter(shape for _ln, shape in hits) == Counter(
        {
            "status-flip": 14,
            "canonical-write": 3,
            "canonical-write-import": 1,
            "canonical-write-getattr": 2,
        }
    )


_C1_DATAFLOW_SITES = Counter(
    {
        ("anchoring.py", "__init__"): 1,
        ("sync.py", "_set_leaf_status"): 1,
        ("sync.py", "_repoint_communities"): 1,
    }
)


def _c1_dataflow_status_assignment_sites(
    src: str, filename: str = "<synthetic>"
) -> list[tuple[int, str | None]]:
    """Fix round 3, item 2a: every ``.status`` Attribute-target assignment (tuple/list
    targets flattened via ``_flatten_targets``, same as ``_c1_hits_in_source``'s own
    Attribute-target branch) whose value ``_status_flip_value_caught`` does NOT flag --
    the dataflow gap that same branch's enum-mention/record-status-string check cannot
    close: a value arriving through a local name (``b.status = status``) mentions no
    enum and spells no record-status string. Each hit carries its enclosing function via
    ``_enclosing_function`` (the same idiom C-3's own site pin uses,
    ``test_c3_accepted_construction_sites_pinned_by_file_and_function`` above).

    Narrowed to a target whose ``attr`` is literally ``"status"`` -- unlike
    ``_c1_hits_in_source``'s own Attribute-target branch, which stays deliberately
    attr-name-agnostic because it is gated by the rare enum-mention/record-status-string
    VALUE condition, this collector inverts that condition (looking for values that
    AREN'T caught), so without the attr-name filter it would flag every ordinary
    ``self.<anything> = <local>`` assignment in the tree -- measured: 55 sites across 9
    files on the real tree without this filter, none of them a status field."""
    tree = ast.parse(src, filename=filename)
    hits: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        status_targets = [
            t
            for t in _flatten_targets(targets)
            if isinstance(t, ast.Attribute) and t.attr == "status"
        ]
        if not status_targets:
            continue
        if not _status_flip_value_caught(node.value):
            hits.append((node.lineno, _enclosing_function(tree, node.lineno)))
    return hits


def _c1_dataflow_status_assignment_hits() -> list[tuple[Path, int, str | None]]:
    """Fix round 3, item 2a's real driver: ``_c1_dataflow_status_assignment_sites`` over
    every file in ``src/sidegraph/`` excluding ``store.py`` -- mirrors
    ``_c1_status_flip_or_canonical_write_hits``'s own exclusion (the only module allowed
    to flip a record's status)."""
    hits: list[tuple[Path, int, str | None]] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.relative_to(_SRC) == Path("store.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, fn in _c1_dataflow_status_assignment_sites(text, filename=str(path)):
            hits.append((path, lineno, fn))
    return hits


def test_c1_dataflow_status_sites_pinned_by_file_and_function():
    """Fix round 3, item 2a: C1's enum-mention/record-status-string check cannot close a
    ``.status`` assignment whose value arrives through a local name -- fix round 2 first
    measured exactly three such sites outside ``store.py`` and recorded a
    ``(file, function)`` site pin as an unbuilt follow-up rather than build it there. This
    is that pin, re-derived by an AST walk THIS session (not copied from that round's
    docstring): all three are legitimate ``AnchorBinding``/``AnchorResolution`` status
    transitions, never a ``Decision``/``Domain`` status flip.

    Keyed on a ``Counter`` of ``(path relative to _SRC, function)`` -- NOT ``p.name``/a
    bare ``set`` (fix round 1 review, Major 1): a basename-only, count-less set collapses
    a SECOND hit under an already-pinned key into that same key (a hurried edit adding a
    second ``.status = <local>`` next to an already-pinned one inside
    ``sync._set_leaf_status`` went undetected), and collides two files sharing a basename
    in different subpackages (a hypothetical ``engine/sync.py`` colliding with
    ``sync.py``). A ``Counter`` catches both: an extra hit under an existing key raises
    that key's count past 1, and a same-named function in a same-basename file under a
    different relative path is a DIFFERENT key entirely. The C-3 idiom this pin already
    cites (``test_c3_accepted_construction_sites_pinned_by_file_and_function``) asserts a
    count for exactly this reason; this pin now does too. A NEW ``.status`` assignment
    landing in this same gap anywhere else outside ``store.py`` -- including a second one
    under a key already pinned -- is a Counter mismatch instead of a silent pass. What it
    does NOT catch, stated because the earlier wording implied otherwise: REPLACING one of
    the three pinned assignments in place (``b.status = status`` becoming
    ``decision.status = status`` inside the same function) leaves the count at 1 and stays
    green. Keying on the statement text would close that; it is not done, because a pin
    that fires on every rewording of a line it already covers is a pin people delete."""
    hits = _c1_dataflow_status_assignment_hits()
    lines_by_key: dict[tuple[str, str | None], list[int]] = {}
    for p, ln, fn in hits:
        lines_by_key.setdefault((str(p.relative_to(_SRC)), fn), []).append(ln)
    found = Counter({key: len(lns) for key, lns in lines_by_key.items()})
    if found == _C1_DATAFLOW_SITES:
        return
    added = found - _C1_DATAFLOW_SITES
    missing = _C1_DATAFLOW_SITES - found

    def _by_path_then_fn(pair: tuple[str, str | None]) -> tuple[str, str]:
        return (pair[0], pair[1] or "")

    added_desc = "; ".join(
        f"{path}:{','.join(str(ln) for ln in lines_by_key[(path, fn)])} ({fn})"
        for path, fn in sorted(added, key=_by_path_then_fn)
    )
    missing_desc = "; ".join(f"{path} ({fn})" for path, fn in sorted(missing, key=_by_path_then_fn))
    parts = []
    if added_desc:
        parts.append(
            f"new/extra .status dataflow site(s) -- justify and pin, or route through "
            f"Store.ratify/ratify_fact/ratify_domains instead: {added_desc}"
        )
    if missing_desc:
        parts.append(f"pinned site(s) gone or renamed -- update the pin: {missing_desc}")
    pytest.fail(" | ".join(parts))


def test_c1_dataflow_checker_bites_on_a_synthetic_local_name_assignment():
    """Proves ``_c1_dataflow_status_assignment_sites`` is not vacuously always-empty (nor
    vacuously always-full): a ``.status`` assignment whose value arrives through a local
    name is reported, pinned to its enclosing function -- while a shape
    ``_c1_hits_in_source`` already catches (a bare enum reference) sitting right next to
    it in the SAME function is not, since ``_status_flip_value_caught`` already flags
    that one, so it is not a dataflow gap."""
    synthetic = (
        "def _fake(d, local_status):\n"
        "    d.status = DecisionStatus.ACCEPTED\n"  # caught by _c1_hits_in_source, not here
        "    d.status = local_status\n"  # the dataflow gap
    )
    hits = _c1_dataflow_status_assignment_sites(synthetic)
    assert hits == [(3, "_fake")]


def _c2_hits_in_source(src: str, filename: str = "<synthetic>") -> list[tuple[int, str | None]]:
    """Collect C2's forbidden shape within one raw source string (parsed here, not
    pre-parsed by the caller) -- factored out so the real driver
    (``_c2_ratifier_identity_call_hits`` below, which adds the file walk) and the
    synthetic-mutation companion test share the IDENTICAL collector. Shape: ANY ``ast.Name``
    or ``ast.Attribute`` node named ``_ratifier_identity``, or an ``ast.ImportFrom`` pulling
    that name out of anywhere (Minor 4) -- not just a bare call, the collector's original
    narrower shape. Today's three sites are unqualified calls (``_ratifier_identity(actor)``
    inside ``store.py``, an ``ast.Name`` used as a call's ``func``), so this widening does
    not change their count; it additionally closes `store_mod._ratifier_identity(a)``
    (attribute access), ``who = _ratifier_identity`` (aliasing), and
    ``functools.partial(_ratifier_identity)`` -- all three carry an ``ast.Name`` or
    ``ast.Attribute`` node spelling the name and so are already caught by this ONE rule,
    with no special-casing per evasion shape. Each hit carries its enclosing function name
    via ``_enclosing_function`` (shared with C-3 above, not a second implementation).

    Still not exhaustive: a ``getattr(module, "_ratifier_identity")`` string-keyed lookup,
    or an identifier assembled at runtime by string concatenation, carries no ``ast.Name``/
    ``ast.Attribute`` node spelling ``_ratifier_identity`` and evades every shape above --
    the same class of gap ``_t7b_alias_and_getattr_hits`` documents for
    ``getattr(store, "rat" + "ify")``. A wholesale reimplementation of the identity-
    resolution logic under a different name is beyond any mechanism guard by construction."""
    tree = ast.parse(src, filename=filename)
    hits: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        is_name_ref = isinstance(node, ast.Name) and node.id == "_ratifier_identity"
        is_attr_ref = isinstance(node, ast.Attribute) and node.attr == "_ratifier_identity"
        is_import_ref = isinstance(node, ast.ImportFrom) and any(
            alias.name == "_ratifier_identity" for alias in node.names
        )
        if is_name_ref or is_attr_ref or is_import_ref:
            hits.append((node.lineno, _enclosing_function(tree, node.lineno)))
    return hits


def _c2_ratifier_identity_call_hits() -> list[tuple[Path, int, str | None]]:
    """C2's real driver: ``_c2_hits_in_source`` over every file in ``src/sidegraph/`` --
    unlike C1, no file is excluded, since the three legitimate call sites themselves live
    in ``store.py``."""
    hits: list[tuple[Path, int, str | None]] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, fn in _c2_hits_in_source(text, filename=str(path)):
            hits.append((path, lineno, fn))
    return hits


def test_c2_ratifier_identity_called_only_in_the_three_c2_transitions():
    """C2: ``store._ratifier_identity`` is referenced (called, aliased, accessed via
    attribute, or imported) in exactly the three C-2 transitions
    (``Store.ratify``/``ratify_fact``/``ratify_domains``), zero references anywhere else --
    pinned by the SET of enclosing function names, not a bare count of 3 (same reasoning as
    T7(b)'s per-method assertion above: a bare count cannot distinguish today's healthy
    state from "one transition lost its call while a fourth site gained one"). Exactly 3
    today, all in ``store.py`` (measured zero elsewhere on the tree at HEAD) -- this is a
    GUARD, not evidence: it is green against today's correct code and can never go red
    against it. ``test_c2_checker_bites_on_a_synthetic_call_outside_the_three_transitions``
    below proves the same collector is not vacuously always-empty."""
    hits = _c2_ratifier_identity_call_hits()
    pairs = [(p, ln) for p, ln, _fn in hits]
    outside = [(p, ln) for p, ln, _fn in hits if p.relative_to(_SRC) != Path("store.py")]
    assert not outside, (
        f"_ratifier_identity referenced outside store.py -- route it through "
        f"Store.ratify/ratify_fact/ratify_domains, the three C-2 transitions, instead: "
        f"{_fmt_hits(outside)}"
    )
    assert len(hits) == 3, f"expected exactly 3 references, found: {_fmt_hits(pairs)}"
    fns = sorted(fn for _p, _ln, fn in hits if fn is not None)
    assert fns == ["ratify", "ratify_domains", "ratify_fact"], (
        f"expected ratify/ratify_domains/ratify_fact each exactly once, found: {fns}"
    )


def test_c2_checker_bites_on_a_synthetic_call_outside_the_three_transitions():
    """Proves ``_c2_hits_in_source`` -- the SAME collector ``test_c2_...`` above runs over
    real source -- is not vacuously always-empty: a synthetic function exercising every
    reference shape (the original bare call plus every Minor-4 widening -- attribute
    access, aliasing, ``functools.partial``, and an import) is reported, all five times,
    each with the correct enclosing-function name. Not a mutation of a real source file
    (item C's brief)."""
    synthetic = (
        "def _rogue():\n"
        "    from .store import _ratifier_identity\n"  # import reference
        "    x = _ratifier_identity(actor)\n"  # bare call
        "    y = store_mod._ratifier_identity(actor)\n"  # attribute call
        "    who = _ratifier_identity\n"  # aliasing
        "    g = functools.partial(_ratifier_identity)\n"  # functools.partial
    )
    hits = _c2_hits_in_source(synthetic)
    assert len(hits) == 5, f"expected 5 references, found: {hits}"
    assert all(fn == "_rogue" for _ln, fn in hits), hits


# -- T7(d): behavioral pin (rev 11, checkpoint-3 external review) -- T7(a)-(c)/C-3 above are
# static mechanism guards over the SOURCE; none of them pins what the human CLI/MCP ratify
# paths actually WRITE. The checkpoint-3 mutant (`store.ratify(id_, **{"actor": "auto:" +
# "test"})` at the human CLI accept call, `cli.py:209`) survived all five of them plus
# `tests/test_cli_ratify.py` + `tests/test_doctor_auto_share.py` + this module's other 152
# tests, because nothing asserted the human paths' `ratified_by` value. These six tests do:
# for each of the human CLI (`cli.ratify_main --accept`) and human MCP (`server._ratify_impl`)
# doors, over a proposed decision/fact/domain, `sidegraph.store._ratifier_identity` is faked
# (the `_fake_ratifier_identity` shape from `tests/test_doctor_auto_share.py` -- an explicit
# `actor` still wins verbatim, only the git-lookup fallback is faked) to a fixed human name,
# and the record read back from a REOPENED store must carry that name, never an `auto:`
# prefix. Only the CLI-decision case below was landed red-first against the checkpoint-3
# mutant; the other five pin the same property over the other five (path, kind) pairs as
# guards, not separately proven red -- each says so in its own docstring.

_T7D_HUMAN = "T7d Human Ratifier"


def _t7d_fake_ratifier_identity(actor: str | None = None) -> str:
    """Copy of ``tests/test_doctor_auto_share.py``'s ``_fake_ratifier_identity`` SHAPE
    (never a lambda that ignores ``actor``): a non-blank ``actor`` still wins verbatim,
    same contract as the real ``sidegraph.store._ratifier_identity`` -- only the
    git-lookup fallback used for a genuine human ratify is faked, to a name fixed for
    this pin so the assertions below have something stable to check against."""
    return actor if actor else _T7D_HUMAN


def _t7d_assert_human_stamp(store_path: Path, getter, id_: str) -> None:
    """Reopen the store fresh -- never trust the in-process object the seeding/ratify
    calls already touched -- and assert the human stamp landed, never an ``auto:`` one.
    ``getter`` is an unbound ``Store`` method (``Store.get_decision`` / ``.get_fact`` /
    ``.get_domain``) so one helper covers all three record kinds. Also asserts the
    record actually reached ``accepted`` (checkpoint-3 fix round 3, out-of-scope
    observation 1) -- all eight T7(d) pins then prove acceptance explicitly, rather than
    only through the "stamp is written only alongside the flip to accepted" invariant."""
    with Store(store_path) as reopened:
        record = getter(reopened, id_)
        assert record.status.value == "accepted"
        assert record.ratified_by == _T7D_HUMAN
        assert not (record.ratified_by or "").startswith("auto:")


def test_t7d_cli_accept_decision_stores_human_identity_not_auto_stamp(tmp_path, monkeypatch):
    """T7(d) RED-FIRST case (fix brief): the human CLI accept path
    (``cli.ratify_main --accept``) for a proposed DECISION stores the faked human
    identity, never an ``auto:`` stamp. Landed red against the checkpoint-3 mutant
    (``store.ratify(id_, **{"actor": "auto:" + "test"})`` at ``cli.py:209``), which makes
    ``_t7d_assert_human_stamp`` fail on ``ratified_by == "auto:test"`` instead."""
    from sidegraph.cli import ratify_main

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    d = _proposed(seed, "t7d cli decision")
    assert ratify_main(["--db", str(store_path), "--accept", d.id]) == 0
    _t7d_assert_human_stamp(store_path, Store.get_decision, d.id)


def test_t7d_cli_accept_fact_stores_human_identity_not_auto_stamp(tmp_path, monkeypatch):
    """Guard: same property as the decision case above, over the human CLI accept path
    for a proposed standalone FACT -- not separately proven red (the checkpoint-3 mutant
    lives on the decision transition specifically); this is what would catch a sibling
    defect on the fact transition."""
    from sidegraph.cli import ratify_main

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    f = _proposed_fact(seed, "t7d cli fact")
    assert ratify_main(["--db", str(store_path), "--accept", f.id]) == 0
    _t7d_assert_human_stamp(store_path, Store.get_fact, f.id)


def test_t7d_cli_accept_domain_stores_human_identity_not_auto_stamp(tmp_path, monkeypatch):
    """Guard: same property, over the human CLI accept path for a proposed DOMAIN -- not
    separately proven red (see the decision case's docstring)."""
    from sidegraph.cli import ratify_main

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    dm = _proposed_domain(seed, slug="t7d-cli-domain")
    assert ratify_main(["--db", str(store_path), "--accept", dm.domain_id]) == 0
    _t7d_assert_human_stamp(store_path, Store.get_domain, dm.domain_id)


def test_t7d_mcp_ratify_decision_stores_human_identity_not_auto_stamp(tmp_path, monkeypatch):
    """Guard: same property, over the human MCP ratify path (``server._ratify_impl``) for
    a proposed DECISION -- the MCP door onto the exact transition the checkpoint-3 mutant
    forges; not separately proven red (see the CLI decision case's docstring)."""
    from sidegraph.server import _ratify_impl

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    d = _proposed(seed, "t7d mcp decision")
    out = _ratify_impl(seed, accept=[d.id], drop=None, reader=None)
    assert out[d.id] == "accepted"
    _t7d_assert_human_stamp(store_path, Store.get_decision, d.id)


def test_t7d_mcp_ratify_fact_stores_human_identity_not_auto_stamp(tmp_path, monkeypatch):
    """Guard: same property, over the human MCP ratify path for a proposed FACT -- not
    separately proven red (see the CLI decision case's docstring)."""
    from sidegraph.server import _ratify_impl

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    f = _proposed_fact(seed, "t7d mcp fact")
    out = _ratify_impl(seed, accept=[f.id], drop=None, reader=None)
    assert out[f.id] == "accepted"
    _t7d_assert_human_stamp(store_path, Store.get_fact, f.id)


def test_t7d_mcp_ratify_domain_stores_human_identity_not_auto_stamp(tmp_path, monkeypatch):
    """Guard: same property, over the human MCP ratify path for a proposed DOMAIN -- not
    separately proven red (see the CLI decision case's docstring)."""
    from sidegraph.server import _ratify_impl

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    dm = _proposed_domain(seed, slug="t7d-mcp-domain")
    out = _ratify_impl(seed, accept=[dm.domain_id], drop=None, reader=None)
    assert out[dm.domain_id] == "accepted"
    _t7d_assert_human_stamp(store_path, Store.get_domain, dm.domain_id)


# -- Checkpoint-3 fix round 2, Minor 1(b): two more human ratify call sites the original six
# T7(d) pins missed -- both found by the review reading src/ for OTHER `store.ratify*` call
# sites, not by the AST/grep guards (which only prove where `actor` is passed, not which
# callers exist at all). Same fixed-human-identity property, same "guard, not separately
# proven red" status as the five non-CLI-decision pins above.


def test_t7d_mcp_ratify_decisions_legacy_stores_human_identity_not_auto_stamp(
    tmp_path, monkeypatch
):
    """Guard (Minor 1b): ``server._ratify_decisions_impl`` (``server.py:1759``, the
    testable core of the LEGACY MCP ``ratify_decisions`` tool) is a separate human
    ``store.ratify`` call site from ``_ratify_impl``/``_ratify_one`` -- not one of the
    original six T7(d) pins. Same property, over a proposed DECISION. Not separately
    proven red (see the CLI decision case's docstring)."""
    from sidegraph.server import _ratify_decisions_impl

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    seed = Store(store_path)
    d = _proposed(seed, "t7d legacy mcp decision")
    out = _ratify_decisions_impl(seed, accept=[d.id])
    assert out[d.id] == "accepted"
    _t7d_assert_human_stamp(store_path, Store.get_decision, d.id)


def test_t7d_doc_import_ratify_matching_proposal_stores_human_identity_not_auto_stamp(
    tmp_path, monkeypatch
):
    """Guard (Minor 1b): the un-pinned ``store.ratify(match.id)`` call inside
    ``_apply_doc_state`` (``doc_import.py:2025``, gated on
    ``request.ratify_matching_proposal``) -- a re-imported doc that lands ACCEPTED
    ratifies its own matching PROPOSED decision. Fixture copied from
    ``tests/test_bootstrap_apply.py::test_accepting_matching_proposal_ratifies_it_repairs_bindings_and_cascades_facts``
    (its ``write_graph``/``request_for_status`` helpers, reused here by cross-module
    import per this file's own established pattern -- see the ``tests.test_doc_import``
    imports at the top of this module). Same property, over that call site. Not
    separately proven red (see the CLI decision case's docstring)."""
    from sidegraph.doc_import import apply_doc_candidate
    from tests.test_bootstrap_apply import request_for_status, write_graph

    monkeypatch.setattr("sidegraph.store._ratifier_identity", _t7d_fake_ratifier_identity)
    store_path = tmp_path / "s"
    store = Store(store_path)
    reader = GraphifyReader(write_graph(tmp_path))
    request = request_for_status(DecisionStatus.ACCEPTED).model_copy(
        update={"ratify_matching_proposal": True}
    )
    proposal = store.add_decision(
        Decision(
            title=request.parsed.title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context=request.parsed.context,
            choice=request.parsed.choice,
            rejected=request.parsed.rejected,
            consequences=request.parsed.consequences,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="doc-import", ref=request.ref),
        )
    )
    result = apply_doc_candidate(store, reader, request)
    assert result.action == "skipped-existing"
    assert result.decision_id == proposal.id
    _t7d_assert_human_stamp(store_path, Store.get_decision, proposal.id)


# ── Task 7: T9 -- manual render parity across two clocks + a live auto policy ────────
#
# All six wiring sites and the eligibility/cascade machinery exist now (Tasks 2-6); the
# point of T9 is that NONE of it is reachable from the READ path. `manual` (the default)
# must render byte-identical to the pre-feature golden regardless of (a) how much wall
# time has passed since the fixture was built, and (b) whether a live non-`manual`
# policy happens to sit in the process environment while the render runs. A diff here is
# a production leak to FIX, never a golden to regenerate or a test to weaken (dispatch
# context).


def test_manual_render_parity_vs_goldens(tmp_path):
    """T9: rebuild the frozen fixture from scratch under two different "now"s -- the real
    current time and 200 days later -- and re-render all four T9 surfaces each time.
    Each shift gets its OWN store (a second build inside the same directory would reuse
    ``_golden_store``'s fixed ``golden.db``/ULIDs, per the pre-flight review's Q1), and
    both the store's own ``valid_from`` values and the render's frozen clock move
    together by the same ``shift`` -- exactly what a real deployment's fixture would look
    like some months later, not a database frozen in time while only the calendar moves
    (that divergent case is ``test_manual_render_parity_bites_when_the_clock_actually_
    shifts`` below). Both shifts must equal the SAME committed
    ``tests/fixtures/auto_policy/parity_goldens.json`` byte-for-byte.

    The second assertion per shift (amendment 3, REQUIRED, not optional) is the spec's
    declared T9 red target: an auto-ratification leak into the read path. It re-renders
    the identical store/clock with ``SIDEGRAPH_RATIFY_POLICY=auto-all`` live in the
    environment via ``_render_parity_surfaces``'s ``extra_env`` -- ``retrieval.py`` and
    ``host/hooks.py`` never read that variable (only ``server.py``/``cli.py`` do, at the
    six write-time wiring sites), so the render must still equal the golden. A future
    change that made either module consult the policy on the read side would fail here
    first."""
    base = datetime.now(UTC)
    golden = json.loads(GOLDENS.read_text())
    reader = GraphifyReader(GOLDEN_GRAPH)
    for shift in (timedelta(0), timedelta(days=200)):
        now = base + shift
        store = _golden_store(tmp_path / f"clock{shift.days}", now)

        rendered = _render_parity_surfaces(store, reader, GOLDEN_GRAPH, now=now)
        assert rendered == golden, f"manual render parity diverged under shift={shift.days}d"

        rendered_under_auto_all = _render_parity_surfaces(
            store,
            reader,
            GOLDEN_GRAPH,
            now=now,
            extra_env={"SIDEGRAPH_RATIFY_POLICY": "auto-all"},
        )
        assert rendered_under_auto_all == golden, (
            "read-path leak: a live SIDEGRAPH_RATIFY_POLICY=auto-all changed the render "
            f"at shift={shift.days}d"
        )


def test_manual_render_parity_bites_when_the_clock_actually_shifts(tmp_path):
    """T9 calendar mutation, kept as a PERMANENT regression proof (pre-flight review Q1),
    following the precedent of ``test_t7c_checker_bites_on_a_synthetic_status_flip``: a
    test that proves the harness above would actually notice a divergence, rather than
    passing vacuously because nothing ever differs.

    Builds the store's clock at ``now = base + 200 days`` (so the 3 accepted decisions
    render at their normal "40 days ago" relative age) but pins the proposed records'
    ``valid_from`` to the ABSOLUTE ``base - 3 days`` -- never a second, un-shifted
    ``datetime.now()`` call, which the pre-flight review's probe found goes red at
    shift=0 too, for the wrong reason (the builder's own clock races the test's). Relative
    to the render clock (``base + 200d``), that absolute date is 203 days old: past the
    default proposal window, and reported as such by the SessionStart nudge. A correct
    two-clock swap MUST produce both differences below; if ``_golden_store`` or
    ``_render_parity_surfaces`` silently fell back to real wall-clock time anywhere on
    this path, the "203 days" figure (and the window drop) would not appear."""
    base = datetime.now(UTC)
    now = base + timedelta(days=200)
    golden = json.loads(GOLDENS.read_text())
    reader = GraphifyReader(GOLDEN_GRAPH)
    store = _golden_store(tmp_path, now, proposed_from=base - timedelta(days=3))

    rendered = _render_parity_surfaces(store, reader, GOLDEN_GRAPH, now=now)

    assert rendered["session_start"] != golden["session_start"]
    assert "oldest 203 days" in rendered["session_start"]
    assert "## Unratified proposals" in golden["task_context"]
    assert "## Unratified proposals" not in rendered["task_context"]


# ── Task 7: T10 -- regulated mode (SIDEGRAPH_UNRATIFIED=off) + auto ratification ─────
#
# Guard: no red target (pre-flight review Q2 probe found no leak). An auto-ratified
# record is ordinary ACCEPTED canonical state by the time any surface renders it -- it
# was never `[unratified]` content, so regulated mode correctly leaves it visible on
# every surface while it keeps hiding whatever the policy left PROPOSED.


def test_regulated_mode_hides_proposed_but_shows_auto_ratified(store, tmp_path, monkeypatch):
    """T10: an ``auto-low-risk`` batch containing one admitted gotcha and one
    kind-gated adr (stays proposed), plus an ``auto-low-risk`` standalone fact and a
    ``manual`` standalone fact, all anchored to the same live entity. The env-unset
    control arm proves the two proposals really do surface under the default -- without
    it, the regulated-mode arm's "these are hidden" assertions would prove nothing (Q2).
    Then, under ``SIDEGRAPH_UNRATIFIED=off``: the task context, ``session_start``'s
    ``additionalContext`` (counter line kept -- it is metadata, not proposal content),
    the two raw MCP listings, the PreToolUse title helper, and the ``list_proposed``
    queue text are all asserted exactly as the pre-flight review's live probe recorded."""
    from sidegraph.host import hooks
    from sidegraph.server import (
        _get_task_context_impl,
        _list_facts_impl,
        _list_proposed_impl,
        _retrieve_decisions_impl,
    )

    reader = _feature_graph(tmp_path)
    auto_gotcha, proposed_adr = capture.propose(
        [
            _eligible_decision_draft(title="auto gotcha"),
            _eligible_decision_draft(title="proposed adr", kind="adr"),
        ],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    [auto_fact] = capture.propose_facts(
        [_eligible_fact_draft(statement="auto fact")],
        store,
        reader,
        ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
    )
    [proposed_fact] = capture.propose_facts(
        [_eligible_fact_draft(statement="proposed fact")], store, reader
    )

    # Canonical stamps first -- the fixture's own shape, before any render is checked.
    stored_gotcha = store.get_decision(auto_gotcha.decision_id)
    assert stored_gotcha.ratified_by == "auto:auto-low-risk"
    assert stored_gotcha.status == DecisionStatus.ACCEPTED
    assert store.get_decision(proposed_adr.decision_id).status == DecisionStatus.PROPOSED
    assert store.get_fact(auto_fact.fact_id).status == DecisionStatus.ACCEPTED
    assert store.get_fact(proposed_fact.fact_id).status == DecisionStatus.PROPOSED

    # -- control arm: env unset -- both proposals surface, tagged [unratified] ---------
    ctx = _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert "proposed adr" in ctx
    assert "proposed fact" in ctx
    assert "[unratified]" in ctx

    # -- regulated arm ------------------------------------------------------------------
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")

    ctx = _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert "auto gotcha" in ctx
    assert "auto fact" in ctx
    assert "proposed adr" not in ctx
    assert "proposed fact" not in ctx
    assert "[unratified]" not in ctx
    assert "## Unratified proposals" not in ctx

    monkeypatch.setenv("SIDEGRAPH_DB", str(store.path))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(reader.path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hooks.session_start()
    session_ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "auto gotcha" in session_ctx
    assert "auto fact" in session_ctx
    assert "proposed adr" not in session_ctx
    assert "proposed fact" not in session_ctx
    assert "2 record(s) awaiting ratification (1 decisions, 1 facts, 0 domains" in session_ctx

    assert [d["title"] for d in _retrieve_decisions_impl(store)] == ["auto gotcha"]
    assert [f["statement"] for f in _list_facts_impl(store)] == ["auto fact"]
    assert hooks._titles_for_path(store, "trader/exec.py") == ["auto gotcha"]

    queue_text = _list_proposed_impl(store)
    assert "proposed adr" in queue_text
    assert "auto gotcha" not in queue_text


# ── Task 7: T19 -- manual compat boundary (CLI batch summaries + MCP result schema) ──
#
# Both halves are GUARDS at HEAD (pre-flight review Q3/Q4): the CLI segment already
# splices behind `!= RatifyPolicy.MANUAL` (Task 5) and the two additive MCP result
# fields already exist (Task 4). Neither half is the sole thing standing between its
# named mutant and a green suite -- each docstring says so rather than claiming
# otherwise.


def test_manual_cli_batch_summaries_keep_the_existing_text(tmp_path, capsys, monkeypatch):
    """T19 CLI half: under an EXPLICIT ``SIDEGRAPH_RATIFY_POLICY=manual``, the three CLI
    batch summary lines this feature spliced a conditional ``, auto-ratified N`` segment
    into (Task 5) print BYTE-IDENTICAL to the pre-feature strings recorded in the ledger
    (``progress.md:52-61``, checked against ``git show ad4bef2:src/sidegraph/cli.py`` --
    exact full-line equality, never a substring pin), and neither stdout nor stderr
    contains ``"auto-ratif"`` anywhere. Fixtures are Task 5's own auto-path tests'
    fixtures, reused here under the opposite policy so the two tests read as a pair
    (pre-flight review Q3): rationale import (``:3598`` companion), doc import
    (``:3631`` companion), and domain bootstrap (``:3690`` companion).

    The mutant this guards against is dropping the ``!= RatifyPolicy.MANUAL`` conditional
    in any of the three printers, making ``auto_segment`` unconditional -- already killed
    by ``tests/test_cli_import.py::test_cli_import_reports_and_writes`` (``:64``),
    ``::test_cli_import_docs_reports_and_writes`` (``:551``), and
    ``tests/test_cli_domains.py::test_cli_domains_bootstrap_reports_and_writes``
    (``:63``); this test is not the sole thing standing between that mutant and a green
    suite."""
    from tests.test_cli_import import _DOC_MENTION_GRAPH, _adr_md
    from tests.test_cli_import import GRAPH as _rationale_graph
    from tests.test_cli_import import _write_md as _cli_write_md
    from tests.test_domains import GRAPH as _bootstrap_graph
    from tests.test_domains import LABELS as _bootstrap_labels

    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "manual")

    # -- rationale import ---------------------------------------------------------------
    graph = tmp_path / "rationale.json"
    graph.write_text(json.dumps(_rationale_graph))
    db = tmp_path / "rationale.db"
    rc = import_main(["--db", str(db), "--graph", str(graph), "--kind", "lesson", "--propose"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "imported 1 decision(s) (skipped: 0 existing, 0 unanchorable)\n"
    assert "auto-ratif" not in out + err

    # -- doc import -----------------------------------------------------------------------
    doc_graph = tmp_path / "doc.json"
    doc_graph.write_text(json.dumps(_DOC_MENTION_GRAPH))
    _cli_write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))
    doc_db = tmp_path / "doc.db"
    rc = import_main(
        [
            "--db",
            str(doc_db),
            "--graph",
            str(doc_graph),
            "--docs",
            str(tmp_path / "docs"),
            "--any-doc",
            "--propose",
        ]
    )
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == (
        "imported 1 decision(s), superseded 0 (skipped: 0 existing, 0 unanchorable, "
        "0 not-decision-shaped, 0 unparseable, 0 superseded-frontmatter, "
        "0 outside-profile)\n"
    )
    assert "auto-ratif" not in out + err

    # -- domain bootstrap -----------------------------------------------------------------
    bootstrap_graph = tmp_path / "bootstrap.json"
    bootstrap_graph.write_text(json.dumps(_bootstrap_graph))
    (tmp_path / ".graphify_labels.json").write_text(json.dumps(_bootstrap_labels))
    bootstrap_db = tmp_path / "bootstrap.db"
    rc = domains_main(["bootstrap", "--db", str(bootstrap_db), "--graph", str(bootstrap_graph)])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "proposed 2 domain(s) (skipped: 0 existing)\n"
    assert "auto-ratif" not in out + err


_T19_PRE_FEATURE_DECISION_KEYS = {
    "status",
    "decision_id",
    "reason",
    "redactions",
    "anchors_skipped",
    "anchors_orphaned",
    "facts",
    "neighbors",
}
_T19_PRE_FEATURE_FACT_KEYS = {
    "status",
    "fact_id",
    "reason",
    "redactions",
    "anchors_skipped",
    "anchors_orphaned",
}
_T19_PRE_FEATURE_DOMAIN_KEYS = {
    "status",
    "domain_id",
    "reason",
    "redactions",
    "warnings",
}
_T19_NEW_KEYS = {"ratified_by", "auto_ratify_error"}


def test_manual_mcp_propose_schema_has_exact_additive_null_fields(store, tmp_path):
    """T19 MCP half: through the two server cores (``_propose_decisions_impl``,
    ``_propose_domains_impl``), the dumped result dict for a decision, its nested
    attached fact, a standalone fact, and a domain gains EXACTLY the two additive keys
    ``ratified_by``/``auto_ratify_error`` over the pre-feature key sets (ledger, Task
    4's ``git show ad4bef2:src/sidegraph/capture.py``) -- never a third. Manual arm: both
    new values are ``None`` for all four shapes. Auto arm (a SEPARATE store, so the
    ``auto-all``-only domain kind and the decision/fact slugs never collide with the
    manual arm's writes): ``ratified_by == "auto:auto-all"`` on all four,
    ``auto_ratify_error is None`` -- per D6 and the pre-flight review's Q4 correction to
    the brief, a SUCCESSFUL auto run leaves ``auto_ratify_error`` null; only a FAILED
    attempt populates it (already pinned at ``:1641``/``:2038``/``:1783``/``:1759``, not
    duplicated here). Guard: the mutant this catches is a third defaulted field added to
    any of the three result models."""
    from sidegraph.server import _propose_decisions_impl, _propose_domains_impl

    reader = _feature_graph(tmp_path)

    # -- manual arm ----------------------------------------------------------------------
    [decision] = _propose_decisions_impl(
        store,
        reader,
        [_eligible_decision_draft(facts=[_eligible_fact_draft(statement="nested fact")])],
    )
    assert set(decision) == _T19_PRE_FEATURE_DECISION_KEYS | _T19_NEW_KEYS
    assert decision["ratified_by"] is None
    assert decision["auto_ratify_error"] is None
    [nested_fact] = decision["facts"]
    assert set(nested_fact) == _T19_PRE_FEATURE_FACT_KEYS | _T19_NEW_KEYS
    assert nested_fact["ratified_by"] is None
    assert nested_fact["auto_ratify_error"] is None

    [standalone_fact] = _propose_decisions_impl(
        store, reader, [], facts=[_eligible_fact_draft(statement="standalone fact")]
    )
    assert set(standalone_fact) == _T19_PRE_FEATURE_FACT_KEYS | _T19_NEW_KEYS
    assert standalone_fact["ratified_by"] is None
    assert standalone_fact["auto_ratify_error"] is None

    [domain] = _propose_domains_impl(store, reader, [_eligible_domain_draft()])
    assert set(domain) == _T19_PRE_FEATURE_DOMAIN_KEYS | _T19_NEW_KEYS
    assert domain["ratified_by"] is None
    assert domain["auto_ratify_error"] is None

    # -- auto arm (separate store) --------------------------------------------------------
    auto_store = Store(tmp_path / "auto.db")

    [auto_decision] = _propose_decisions_impl(
        auto_store,
        reader,
        [_eligible_decision_draft(facts=[_eligible_fact_draft(statement="nested fact")])],
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert set(auto_decision) == _T19_PRE_FEATURE_DECISION_KEYS | _T19_NEW_KEYS
    assert auto_decision["ratified_by"] == "auto:auto-all"
    assert auto_decision["auto_ratify_error"] is None
    [auto_nested_fact] = auto_decision["facts"]
    assert set(auto_nested_fact) == _T19_PRE_FEATURE_FACT_KEYS | _T19_NEW_KEYS
    assert auto_nested_fact["ratified_by"] == "auto:auto-all"
    assert auto_nested_fact["auto_ratify_error"] is None

    [auto_standalone_fact] = _propose_decisions_impl(
        auto_store,
        reader,
        [],
        facts=[_eligible_fact_draft(statement="standalone fact")],
        ratify_policy=RatifyPolicy.AUTO_ALL,
    )
    assert set(auto_standalone_fact) == _T19_PRE_FEATURE_FACT_KEYS | _T19_NEW_KEYS
    assert auto_standalone_fact["ratified_by"] == "auto:auto-all"
    assert auto_standalone_fact["auto_ratify_error"] is None

    [auto_domain] = _propose_domains_impl(
        auto_store, reader, [_eligible_domain_draft()], ratify_policy=RatifyPolicy.AUTO_ALL
    )
    assert set(auto_domain) == _T19_PRE_FEATURE_DOMAIN_KEYS | _T19_NEW_KEYS
    assert auto_domain["ratified_by"] == "auto:auto-all"
    assert auto_domain["auto_ratify_error"] is None
