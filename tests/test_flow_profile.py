import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sidegraph.doc_import import import_docs, parse_decision_doc
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import (
    BMAD_DIALECT,
    GENERIC_ADR_DIALECT,
    GENKOVICH_SDD_DIALECT,
    OPENSPEC_DIALECT,
    PROFILES,
    SPEC_KIT_DIALECT,
    SUPERPOWERS_DIALECT,
    FlowProfile,
    ReaderDialect,
    get_profile,
)
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "flows"


def _read(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


# -- portable core / seam layering contract --------------------------------------------------
#
# `src/sidegraph/*.py` (the flat core) must never import `bootstrap/` — bootstrap is a seam
# that depends on the core, never the other way around. (`host/` carries the same
# one-directional rule, but no core module imports it today, and none of the mechanisms below
# check it — this contract closes only the bootstrap violation this task found; extending it
# to host is a separate, currently-unneeded change.) Three independent mechanisms, because one
# spelling is not the invariant — a single check would miss `from sidegraph import bootstrap`
# (fix round 1, review finding 1), a literal `importlib.import_module(...)` sitting in a
# function nobody calls (fix round 1, review finding 2), or a plain import nested in a function
# body / TYPE_CHECKING block that never runs.

_CORE = Path("src/sidegraph")


def _is_bootstrap_module(name: str | None) -> bool:
    """True for an absolute dotted path naming the bootstrap seam or one of its submodules
    (e.g. "sidegraph.bootstrap", "sidegraph.bootstrap.model")."""
    return name is not None and (
        name == "sidegraph.bootstrap" or name.startswith("sidegraph.bootstrap.")
    )


def _resolve_from_module(node: ast.ImportFrom) -> str | None:
    """The absolute dotted module an `ImportFrom` node imports FROM, for a node written in a
    flat module directly inside the `sidegraph` package. level 0 = absolute (`node.module`
    verbatim); level 1 = relative to the `sidegraph` package itself, since every core file is
    a plain module in that package, not a sub-package — so both `from .bootstrap import X`
    and `from . import bootstrap` resolve through `sidegraph`. Deeper relative levels can't
    occur in a flat package (Python would raise ImportError at runtime), so None is returned
    for those — there is nothing meaningful to resolve."""
    if node.level == 0:
        return node.module
    if node.level == 1:
        return f"sidegraph.{node.module}" if node.module else "sidegraph"
    return None


def _ast_offenders(core_dir: Path) -> list[str]:
    """Mechanism (a): walk the AST of every `*.py` file directly under `core_dir`, visiting
    Import/ImportFrom nodes at EVERY depth (ast.walk covers nodes nested inside a function
    body or a TYPE_CHECKING if-block just as well as module level — it parses the syntax tree
    and does not care whether the surrounding code ever executes). Flags every spelling that
    resolves to the bootstrap seam: `import sidegraph.bootstrap...`, absolute
    `from sidegraph.bootstrap... import X`, absolute `from sidegraph import bootstrap` (review
    finding 1 — this is the one spelling the first version of this function missed, because it
    names the submodule through the imported alias rather than through `node.module`), and the
    two relative spellings a flat core module could write (`from .bootstrap import X`,
    `from . import bootstrap`)."""
    offenders: list[str] = []
    for path in sorted(core_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_bootstrap_module(alias.name):
                        offenders.append(f"{path.as_posix()}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_from_module(node)
                if base is None:
                    continue
                if _is_bootstrap_module(base):
                    offenders.append(f"{path.as_posix()}: from {base} import ...")
                    continue
                for alias in node.names:
                    resolved = f"{base}.{alias.name}"
                    if _is_bootstrap_module(resolved):
                        offenders.append(f"{path.as_posix()}: from {base} import {alias.name}")
    return offenders


def _literal_offenders(core_dir: Path) -> list[str]:
    """Mechanism (c) (added fix round 1, review finding 2): a cheap literal-substring scan of
    each core module's raw source text for "sidegraph.bootstrap". This is deliberately NOT a
    general dynamic-import detector: a COMPUTED module name (string concatenation, an
    f-string, a variable) defeats it exactly like it defeats the AST walk and the runtime
    check in `test_portable_core_never_imports_a_seam_runtime` below — see
    test_probe_computed_module_name_in_an_uncalled_function_escapes_all_three, which pins that
    limit rather than leaving it silently assumed away. What this scan DOES catch, that the
    other two miss: a literal `importlib.import_module("sidegraph.bootstrap...")` (or
    `__import__(...)`) call sitting in a function that is never called at import time — dead
    code, by this project's invariant, is still a violation the moment it names the seam."""
    return [
        path.as_posix()
        for path in sorted(core_dir.glob("*.py"))
        if "sidegraph.bootstrap" in path.read_text(encoding="utf-8")
    ]


def test_portable_core_never_imports_a_seam_ast():
    """Red against unfixed code (2026-08-02, before this task's fix): listed exactly
    src/sidegraph/profiles.py, which imported ProfileDetection from sidegraph.bootstrap.model,
    inverting the core/seam layering that only a TYPE_CHECKING guard in bootstrap/model.py
    kept from cycling."""
    core_files = sorted(_CORE.glob("*.py"))
    assert core_files  # sanity: the glob actually found the flat core, not an empty directory
    assert _ast_offenders(_CORE) == []


def test_portable_core_never_mentions_bootstrap_literally():
    """Mechanism (c) against the real core — see `_literal_offenders`'s docstring for exactly
    what this does and does not cover."""
    core_files = sorted(_CORE.glob("*.py"))
    assert core_files  # sanity: the glob actually found the flat core, not an empty directory
    assert _literal_offenders(_CORE) == []


def test_portable_core_never_imports_a_seam_runtime():
    """Mechanism (b): import each core module alone, in a brand-new Python process (so
    sys.modules starts empty — not polluted by whatever the rest of the test suite already
    imported), and assert no sidegraph.bootstrap* key ever lands in sys.modules.

    Honest scope (corrected fix round 1, review finding 2 — the original docstring overclaimed
    this): this only catches a dynamic import that actually EXECUTES at import time — an
    unconditional `importlib.import_module`/`__import__` call, with any name, literal or
    computed. It has no visibility into code paths that never run: a dynamic import guarded
    inside a function nobody calls at import time is invisible here regardless of whether its
    name is literal or computed. Mechanism (c) above closes the literal-name half of that gap.
    A computed name inside an uncalled function escapes every mechanism in this file — see
    test_probe_computed_module_name_in_an_uncalled_function_escapes_all_three."""
    core_modules = sorted(
        f"sidegraph.{path.stem}" for path in _CORE.glob("*.py") if path.stem != "__init__"
    )
    assert core_modules  # sanity: the glob actually found the flat core
    probe = (
        "import sys\n"
        "import {module}\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'sidegraph.bootstrap' or m.startswith('sidegraph.bootstrap.')\n"
        ")\n"
        "assert not leaked, leaked\n"
    )
    for module in core_modules:
        result = subprocess.run(
            [sys.executable, "-c", probe.format(module=module)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{module} pulled in bootstrap: {result.stderr}"


# -- mechanism boundary tests (fix round 1, code review) --------------------------------------
#
# The five-probe matrix the review asked for, each pinned as its own permanent regression test
# against a synthetic single-file "core" directory (never the real one — these mutate nothing
# in src/sidegraph). Each docstring states which mechanism(s) fire and why. The runtime
# mechanism (b) is exercised only against the real core above — proving its behavior on these
# probes needs a real importable module, which was verified by hand against a disposable git
# worktree (never this checkout) while diagnosing the findings below; see the task report for
# that transcript.


def _probe_dir(tmp_path: Path, source: str) -> Path:
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "probe.py").write_text(source, encoding="utf-8")
    return core_dir


def test_probe_a_module_level_absolute_dotted_import(tmp_path: Path):
    """(a) `import sidegraph.bootstrap.model` at module level. Fires: AST, literal."""
    core_dir = _probe_dir(tmp_path, "import sidegraph.bootstrap.model\n")
    assert _ast_offenders(core_dir) != []
    assert _literal_offenders(core_dir) != []


def test_probe_b_from_sidegraph_import_bootstrap(tmp_path: Path):
    """(b) `from sidegraph import bootstrap`. Review finding 1 (fix round 1): this is the ONE
    absolute spelling the original level-0 branch didn't resolve, because it names the
    submodule through the imported alias, not through `node.module`. Fires: AST (after this
    fix). Does NOT fire the literal scan — the source text has no contiguous
    "sidegraph.bootstrap" substring (there's a space and the word "import" in between) —
    proving the AST fix is load-bearing on its own, not redundant with mechanism (c)."""
    core_dir = _probe_dir(tmp_path, "from sidegraph import bootstrap\n")
    assert _ast_offenders(core_dir) != []
    assert _literal_offenders(core_dir) == []


def test_probe_c_import_nested_in_a_function_body_never_called(tmp_path: Path):
    """(c) a real import statement nested inside a function that is never invoked. Fires:
    AST (ast.walk doesn't care whether the surrounding code ever runs), literal (the dotted
    name is present verbatim in the source)."""
    source = (
        "def never_called():\n"
        "    from sidegraph.bootstrap.model import ProfileDetection\n"
        "    return ProfileDetection\n"
    )
    core_dir = _probe_dir(tmp_path, source)
    assert _ast_offenders(core_dir) != []
    assert _literal_offenders(core_dir) != []


def test_probe_d_import_inside_type_checking_block(tmp_path: Path):
    """(d) an import inside `if TYPE_CHECKING:` — always False at runtime, so nothing here
    ever executes. Isolates the TYPE_CHECKING variable alone (probe (b) above already covers
    the "from sidegraph import bootstrap" spelling at module level), using the dotted absolute
    form so the literal-scan claim below is exact. Fires: AST (parses the block's body
    regardless of the runtime condition), literal (dotted name present verbatim). Empirically
    confirmed by hand (fix round 1) that this shape is invisible to the runtime mechanism
    against a disposable worktree copy of the real repo — see the task report."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sidegraph.bootstrap.model import ProfileDetection\n"
    )
    core_dir = _probe_dir(tmp_path, source)
    assert _ast_offenders(core_dir) != []
    assert _literal_offenders(core_dir) != []


def test_probe_e_lazy_literal_importlib_call_in_an_uncalled_function(tmp_path: Path):
    """(e) review finding 2 (fix round 1): a literal importlib.import_module call sitting in a
    function nobody calls. This is NOT an Import/ImportFrom node (it's a plain Call), so the
    AST walk cannot see it by construction — confirmed here, not assumed. It also never
    executes, so the runtime probe (which only sees modules actually imported) can't see it
    either — confirmed by hand against a disposable worktree copy of the real repo (task
    report). Mechanism (c), the literal scan, is the only one of the three that fires, which
    is exactly why it was added."""
    source = (
        "import importlib\n"
        "def never_called():\n"
        "    return importlib.import_module('sidegraph.bootstrap.model')\n"
    )
    core_dir = _probe_dir(tmp_path, source)
    assert _ast_offenders(core_dir) == []  # confirms the AST walk genuinely can't see this
    assert _literal_offenders(core_dir) != []  # the literal scan does


def test_probe_computed_module_name_in_an_uncalled_function_escapes_all_three(tmp_path: Path):
    """Honest limit, stated plainly rather than rounded up (review: "if anything still
    escapes all three, say so plainly"). A dynamic import whose module name is COMPUTED
    (string concatenation here) AND sits in a function nobody calls at import time defeats
    every mechanism in this file: the AST walk sees a Call, not an Import/ImportFrom, so it
    can't resolve the name even in principle; the literal scan finds no contiguous
    "sidegraph.bootstrap" substring (it's built from two literals concatenated at runtime);
    the runtime probe never executes the function, so sys.modules never sees it. Closing this
    fully would need executing every function reachable from each core module's public
    surface — well beyond what a static/import-time contract test can afford. This test exists
    so that limit is documented and searchable, not silently assumed away."""
    source = (
        "import importlib\n"
        "def never_called():\n"
        "    return importlib.import_module('sidegraph.' + 'bootstrap.model')\n"
    )
    core_dir = _probe_dir(tmp_path, source)
    assert _ast_offenders(core_dir) == []
    assert _literal_offenders(core_dir) == []


def _glob_discovers(profile_name: str, tree_rel: str) -> set[str]:
    """Expand a profile's ingest_globs against a fixture tree shaped like the real flow's
    on-disk layout, returning matched paths relative to that tree.

    This is the check a golden parse test cannot make: a profile whose globs match nothing in
    the real layout still parses fixtures handed to it directly, then silently ingests nothing
    in production (spec §5).
    """
    root = FIXTURES / tree_rel
    found: set[str] = set()
    for pattern in get_profile(profile_name).ingest_globs:
        found |= {str(p.relative_to(root)) for p in root.glob(pattern)}
    return found


def test_genkovich_adr_takes_choice_from_outcome_not_drivers():
    text = _read("genkovich/0001-queue-backpressure.md")
    rel = "docs/features/payments/adr/0001-queue-backpressure.md"
    parsed = parse_decision_doc(text, rel, dialect=GENKOVICH_SDD_DIALECT)
    assert parsed is not None
    assert parsed.title == "0001 — Bound the payment queue with backpressure"
    assert "OOM-killed" in parsed.context
    # Red against the shipped tuple, which led with a bare "decision" and took Decision drivers.
    assert "backpressure to the producer" in parsed.choice
    assert "Memory ceiling" not in parsed.choice
    # Red against the shipped tuple, which had no keyword prefixing "Considered options".
    assert parsed.rejected is not None and "Autoscaling the worker pool" in parsed.rejected
    assert parsed.consequences is not None and "rejection path" in parsed.consequences
    assert parsed.frontmatter_status == "ACCEPTED"


def test_the_sad_needs_this_dialect_and_not_the_default():
    text = _read("genkovich/sad.md")
    rel = "docs/features/payments/sad.md"
    ours = parse_decision_doc(text, rel, dialect=GENKOVICH_SDD_DIALECT)
    assert ours is not None
    assert "producer backpressure" in ours.choice  # 4. Solution strategy
    assert ours.consequences is not None and "idempotent" in ours.consequences  # 11. Risks...
    # The default dialect knows none of arc42's vocabulary. If this ever stops being true the
    # profile has stopped earning its place in the registry.
    default = parse_decision_doc(text, rel, dialect=GENERIC_ADR_DIALECT)
    assert default is None or "producer backpressure" not in (default.choice or "")


def test_genkovich_globs_exclude_spec_by_design():
    # Upstream puts ADRs at docs/features/<slug>/adr/*, NOT docs/adr/*, and writes sad.md
    # beside them — a profile globbing docs/adr/* discovers nothing here.
    found = _glob_discovers("genkovich-sdd", "genkovich/tree")
    assert found == {
        "docs/features/payments/adr/0001-queue-backpressure.md",
        "docs/features/payments/sad.md",
    }
    # spec.md sits right there on disk, in the right place (tree/docs/features/payments/), and
    # is deliberately NOT discovered (owner ruling, 2026-07-27): it is a requirements document
    # with no heading any `choice` keyword can match, and this profile ingests decisions, not
    # requirements. If a future edit "fixes" the glob by adding spec.md back, this line — not
    # just the exact-set assertion above — makes the regression obvious and its reason legible.
    assert "docs/features/payments/spec.md" not in found


def test_spec_kit_maps_summary_and_complexity_tracking():
    text = _read("spec-kit/plan.md")
    rel = "specs/003-payment-retries/plan.md"
    parsed = parse_decision_doc(text, rel, dialect=SPEC_KIT_DIALECT)
    assert parsed is not None
    assert parsed.title == "Implementation Plan: payment retries"
    assert "synchronized retry wave" in parsed.context  # ## Technical Context
    assert "exponential backoff and jitter" in parsed.choice  # ## Summary
    # Complexity Tracking is spec-kit's only place where a discarded option is written down.
    assert parsed.rejected is not None
    assert "dead-letter queue was rejected" in parsed.rejected


def test_spec_kit_plan_has_no_consequences_section_by_design():
    # Verified against upstream (spec-template.md/plan-template.md): neither template has a
    # consequences section at all. A tuple of guessed keywords that match nothing would be a
    # silent hole; `consequences=()` is honest and this pins it as `None`, not a partial match.
    # Pin BOTH: the tuple itself (so restoring the brief's unsourced
    # ("review", "risks", "consequences") is caught even though none of those match this
    # fixture either — the parse assertion alone would stay green through that regression)
    # and the observable parse effect.
    assert SPEC_KIT_DIALECT.consequences == ()
    text = _read("spec-kit/plan.md")
    rel = "specs/003-payment-retries/plan.md"
    parsed = parse_decision_doc(text, rel, dialect=SPEC_KIT_DIALECT)
    assert parsed is not None
    assert parsed.consequences is None


def test_spec_kit_plan_needs_this_dialect_and_not_the_default():
    text = _read("spec-kit/plan.md")
    rel = "specs/003-payment-retries/plan.md"
    ours = parse_decision_doc(text, rel, dialect=SPEC_KIT_DIALECT)
    assert ours is not None
    assert "exponential backoff and jitter" in ours.choice
    assert ours.rejected is not None and "dead-letter queue" in ours.rejected
    # The default dialect knows none of spec-kit's vocabulary — Technical Context and
    # Complexity Tracking are not in GENERIC_ADR_DIALECT at all. If this ever stops being
    # true the profile has stopped earning its place in the registry.
    default = parse_decision_doc(text, rel, dialect=GENERIC_ADR_DIALECT)
    assert default is None or "exponential backoff and jitter" not in (default.choice or "")


def test_spec_kit_globs_find_plan_only_not_spec():
    # Upstream numbers feature directories (specs/<###-feature>/) and puts spec.md right
    # beside plan.md (R3: requirements documents are not ingested) — an exact-set assertion,
    # not membership, so re-adding spec.md to ingest_globs fails this test with a stated
    # reason instead of passing silently.
    found = _glob_discovers("spec-kit", "spec-kit/tree")
    assert found == {"specs/003-payment-retries/plan.md"}
    # spec.md sits right there on disk, in the right place, and is deliberately NOT
    # discovered: it is a requirements document (User Scenarios & Testing / Requirements /
    # Success Criteria / Assumptions) with no heading any `choice` keyword can match. If a
    # future edit "fixes" the glob by adding spec.md back, this line — not just the exact-set
    # assertion above — makes the regression obvious and its reason legible.
    assert "specs/003-payment-retries/spec.md" not in found


def test_bmad_spine_maps_paradigm_and_invariants():
    """Re-pinned by the oneshot+granularity spec (its "tests this spec modifies" table):
    under split, the AD content moves from the PARENT's choice to per-AD child records
    (pinned at their new address in the import tests below); the parent's choice is the
    next matching non-split section — Consistency Conventions."""
    text = _read("bmad/ARCHITECTURE-SPINE.md")
    rel = (
        "_bmad-output/planning-artifacts/architecture/"
        "architecture-payments-2026-07-30/ARCHITECTURE-SPINE.md"
    )
    parsed = parse_decision_doc(text, rel, dialect=BMAD_DIALECT)
    assert parsed is not None
    assert parsed.title == "Architecture Spine — payments-platform"
    assert "Hexagonal" in parsed.context  # ## Design Paradigm
    # Parent choice = Consistency Conventions (the split section is excluded once it
    # produced children); the AD content lives in the child records now.
    assert "money as integer minor units" in parsed.choice
    assert "backpressure" not in parsed.choice
    assert "Hexagonal" not in parsed.choice
    # BMAD's status vocabulary is draft|final; "final" is not a recognized accepted-like
    # value, so it takes the normal --propose-controlled path (an honest reading, pinned).
    assert parsed.frontmatter_status == "final"


def test_bmad_spine_rejected_empty_deferred_maps_to_consequences():
    """Re-pinned by the oneshot+granularity spec: rev-1 pinned `consequences == ()` (the
    pre-E10 verified-empty state); E10 measured the spine's Deferred section as the
    highest-value uncaptured class (5 of 14 memlog losses), so the dialect now maps it.
    `rejected == ()` stays — BMAD still writes rejected alternatives only to the memlog
    (owner ruling variant A, measured in E10 phase 2)."""
    assert BMAD_DIALECT.rejected == ()
    assert BMAD_DIALECT.consequences == ("deferred",)
    text = _read("bmad/ARCHITECTURE-SPINE.md")
    rel = "_bmad-output/planning-artifacts/architecture/x/ARCHITECTURE-SPINE.md"
    parsed = parse_decision_doc(text, rel, dialect=BMAD_DIALECT)
    assert parsed is not None
    assert parsed.rejected is None
    assert parsed.consequences is not None
    assert "Multi-currency settlement" in parsed.consequences


def test_bmad_spine_needs_this_dialect_and_not_the_default():
    text = _read("bmad/ARCHITECTURE-SPINE.md")
    rel = "_bmad-output/planning-artifacts/architecture/x/ARCHITECTURE-SPINE.md"
    ours = parse_decision_doc(text, rel, dialect=BMAD_DIALECT)
    assert ours is not None
    assert "money as integer minor units" in ours.choice  # conventions, a real decision table
    # The default dialect's bare "design" prefix-matches "## Design Paradigm" (doc order,
    # first keyword match) — it takes the one-paragraph paradigm as the whole choice and
    # never reaches conventions or the AD blocks. If this ever stops being true the
    # profile has stopped earning its place in the registry.
    default = parse_decision_doc(text, rel, dialect=GENERIC_ADR_DIALECT)
    assert default is None or "money as integer minor units" not in (default.choice or "")


# -- split-choice (oneshot+granularity spec D2/D3) -------------------------------------------

BMAD_GRAPH = {
    "built_at_commit": "bmadg",
    "nodes": [
        {
            "id": "n-iq",
            "label": "IntakeQueue",
            "norm_label": "IntakeQueue",
            "file_type": "code",
            "source_file": "payments/intake.py",
            "community": "1",
        },
        {
            "id": "n-is",
            "label": "IdempotencyStore",
            "norm_label": "IdempotencyStore",
            "file_type": "code",
            "source_file": "payments/idempotency.py",
            "community": "1",
        },
    ],
    "links": [],
}


def _import_bmad_fixture(tmp_path, store=None):
    reader = _reader(tmp_path, BMAD_GRAPH)
    store = store or Store(tmp_path / "s.db")
    doc = _write_md(tmp_path, "spine/ARCHITECTURE-SPINE.md", _read("bmad/ARCHITECTURE-SPINE.md"))
    report = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True, propose=True)
    return store, reader, str(doc), report


def test_bmad_split_imports_parent_plus_one_record_per_ad(tmp_path):
    """Spec T6/T7: the fixture spine (2 ADs) imports as parent + 2 children; children
    carry the AD content at per-AD refs. Red against unfixed code (single record)."""
    store, _reader_, doc, report = _import_bmad_fixture(tmp_path)
    assert report.imported == 3
    records = list(store.iter_decisions())
    by_ref = {d.provenance.ref: d for d in records}
    assert set(by_ref) == {
        doc,
        f"{doc}#ad-1-bounded-intake-queue-with-producer-backpressure",
        f"{doc}#ad-2-adopted-idempotency-keys-on-every-mutation",
    }
    parent = by_ref[doc]
    assert "money as integer minor units" in parent.choice
    assert "Multi-currency settlement" in (parent.consequences or "")
    c1 = by_ref[f"{doc}#ad-1-bounded-intake-queue-with-producer-backpressure"]
    assert c1.title.startswith("AD-1")
    assert "backpressure" in c1.choice
    assert "Hexagonal" in c1.context  # shared paradigm frame
    assert c1.context.rstrip().endswith(
        "#ad-1-bounded-intake-queue-with-producer-backpressure"
    )  # effective-ref suffix (spec review F13)
    assert c1.rejected is None and c1.consequences is None
    c2 = by_ref[f"{doc}#ad-2-adopted-idempotency-keys-on-every-mutation"]
    assert "idempotency key" in c2.choice

    # Children anchor from their OWN text, the parent from the whole doc (code review F8:
    # pin the NAMES, not just the tier — `any(tier == 2)` was true under either source).
    def tier2_names(record_id):
        names = set()
        for b in store.bindings_for_record(record_id):
            if b.tier == 2:
                e = store.get_entity(b.entity_id)
                names.add(e.canonical_name)
        return names

    assert tier2_names(c1.id) == {"IntakeQueue"}
    assert tier2_names(c2.id) == {"IdempotencyStore"}
    assert tier2_names(parent.id) == {"IntakeQueue", "IdempotencyStore"}


def test_bmad_split_reimport_is_idempotent(tmp_path):
    # Spec T8: same spine again → every record skipped_existing, nothing duplicated.
    store, reader, doc, _ = _import_bmad_fixture(tmp_path)
    report2 = import_docs(store, reader, [doc], profile="bmad", any_doc=True, propose=True)
    assert report2.imported == 0
    assert report2.superseded == 0
    assert report2.skipped_existing == 3
    assert len(list(store.iter_decisions())) == 3


def test_bmad_split_editing_one_ad_supersedes_only_that_child(tmp_path):
    # Spec T9: edit one AD's body → that child superseded; siblings + parent skipped.
    store, reader, doc, _ = _import_bmad_fixture(tmp_path)
    text = Path(doc).read_text()
    Path(doc).write_text(
        text.replace(
            "producers receive backpressure instead of buffering",
            "producers receive backpressure instead of buffering; overflow is rejected",
        )
    )
    report2 = import_docs(store, reader, [doc], profile="bmad", any_doc=True, propose=True)
    assert report2.superseded == 1
    assert report2.skipped_existing == 2
    open_now = [d for d in store.iter_decisions() if d.valid_to is None]
    edited = [
        d
        for d in open_now
        if (d.provenance.ref or "").endswith(
            "#ad-1-bounded-intake-queue-with-producer-backpressure"
        )
    ]
    assert len(edited) == 1
    assert "overflow is rejected" in edited[0].choice


def test_bmad_split_upgrades_a_pre_split_import(tmp_path):
    """Spec T10 — the E10b upgrade path as a unit test: a store holding the OLD one-record
    truncated import at the parent's bare ref gets that record superseded by the new
    parent; the children land fresh. Red against unfixed code."""
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    reader = _reader(tmp_path, BMAD_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _write_md(tmp_path, "spine/ARCHITECTURE-SPINE.md", _read("bmad/ARCHITECTURE-SPINE.md"))
    old = Decision(
        title="Architecture Spine — payments-platform",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="pre-split truncated import",
        choice="### AD-1 — Bounded intake queue …[truncated]",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="doc-import", author="sidegraph-import", ref=str(doc)),
    )
    store.add_decision(old)

    report = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True)
    assert report.superseded == 1  # the parent closes the truncated record
    assert report.imported == 2  # the two children are fresh
    closed = store.get_decision(old.id)
    assert closed.valid_to is not None
    successor = [d for d in store.iter_decisions() if d.supersedes == old.id]
    assert len(successor) == 1 and "money as integer minor units" in successor[0].choice


def test_split_section_without_h3_children_is_ordinary_choice_material(tmp_path):
    """Spec T13 (red against the unconditional-exclusion reading of D2, review F7). The
    fixture MUST carry a `## Consistency Conventions` section (code review F2): without
    it, the unconditional mutant's excluded-section loss was masked by the split_intro
    fallback handing back the same paragraph — the test compared a constant to itself.
    With conventions present, the unconditional reading picks conventions while the
    correct conditional reading keeps the flat rule paragraph."""
    text = (
        "# Spine — flat\n\n"
        "## Design Paradigm\n\nLayered.\n\n"
        "## Invariants & Rules\n\nOne flat rule paragraph about `IntakeQueue` capping.\n\n"
        "## Consistency Conventions\n\nmoney as integer minor units\n\n"
        "## Deferred\n\nNothing yet.\n"
    )
    no_split = BMAD_DIALECT.model_copy(update={"split_choice": ()})
    with_split = parse_decision_doc(text, "x/ARCHITECTURE-SPINE.md", dialect=BMAD_DIALECT)
    without = parse_decision_doc(text, "x/ARCHITECTURE-SPINE.md", dialect=no_split)
    assert with_split is not None and without is not None
    assert with_split.model_dump() == without.model_dump()
    assert "flat rule paragraph" in with_split.choice


def test_split_children_come_only_from_the_split_section(tmp_path):
    """Spec T16 (review F1 — the red that separates the naive positional reading from the
    containment rule): H3s under a LATER, non-split H2 must not become children."""
    # The "## Invariants & Rules" intro paragraph below is load-bearing (E3 blast-radius
    # ruling, 2026-08-06, design/superpowers/specs/2026-08-06-openspec-profile-design.md):
    # without it, `split_intro` is empty, the parent's `choice` falls through to
    # `_deepest_choice_fallback` and lands on "## Design Paradigm"'s own body ("Hexagonal."
    # — the same text `context` uses), and E3 rule 1 correctly suppresses that echoed
    # parent — a real finding this test's fixture was accidentally relying on, not this
    # test's concern (T16's negative on H3-under-a-foreign-H2 is orthogonal to E3). The
    # intro gives the parent a genuine, non-echo choice so it survives E3 unchanged.
    text = (
        "# Spine — scoped\n\n"
        "## Design Paradigm\n\nHexagonal.\n\n"
        "## Invariants & Rules\n\nThese invariants bind every module below.\n\n"
        "### AD-1 — one\n\n- **Rule:** `IntakeQueue` is capped\n\n"
        "## Structural Seed\n\n"
        "### module-a\n\nabout `IntakeQueue`\n\n"
        "### module-b\n\nabout `IdempotencyStore`\n\n"
        "## Deferred\n\nlater.\n"
    )
    reader = _reader(tmp_path, BMAD_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _write_md(tmp_path, "spine2/ARCHITECTURE-SPINE.md", text)
    report = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True, propose=True)
    refs = {d.provenance.ref for d in store.iter_decisions()}
    assert f"{doc}#ad-1-one" in refs
    assert not any("#module-a" in r or "#module-b" in r for r in refs)
    assert report.imported == 2  # parent + the one real child


def test_split_colliding_slugs_get_ordinal_suffixes(tmp_path):
    """Spec T15 (review F8): two H3s that slugify identically → distinct refs, both
    imported, neither supersedes the other. Red against unfixed code (no collision rule
    existed)."""
    text = (
        "# Spine — collide\n\n"
        "## Design Paradigm\n\nHexagonal.\n\n"
        "## Invariants & Rules\n\n"
        "### AD-3 — Strategy dual-mode contract\n\n- **Rule:** `IntakeQueue` one\n\n"
        "### AD-3: Strategy dual-mode contract\n\n- **Rule:** `IdempotencyStore` two\n\n"
        "## Deferred\n\nlater.\n"
    )
    reader = _reader(tmp_path, BMAD_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _write_md(tmp_path, "spine3/ARCHITECTURE-SPINE.md", text)
    report = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True, propose=True)
    refs = sorted(
        d.provenance.ref for d in store.iter_decisions() if "#" in (d.provenance.ref or "")
    )
    assert len(refs) == 2 and refs[0] != refs[1]
    assert refs[1].endswith("-2")
    assert report.superseded == 0
    assert all(d.supersedes is None for d in store.iter_decisions())


def test_split_natural_dash2_slug_does_not_collide(tmp_path):
    """Code review F1 (BLOCKING): a heading whose slug is NATURALLY `<base>-2` collided
    with the second occurrence of `<base>` under the per-base counter — one AD silently
    superseded its sibling on a fresh store and flip-flopped on every re-run. Uniqueness
    is now a post-condition over emitted fragments. Red against the counter design."""
    text = (
        "# Spine — natural collision\n\n"
        "## Design Paradigm\n\nHexagonal.\n\n"
        "## Invariants & Rules\n\n"
        "### Rule\n\n- **Rule:** `IntakeQueue` alpha\n\n"
        "### Rule\n\n- **Rule:** `IdempotencyStore` beta\n\n"
        "### Rule 2\n\n- **Rule:** `IntakeQueue` gamma — a DISTINCT third rule\n\n"
        "## Consistency Conventions\n\nmoney as minor units\n\n"
    )
    reader = _reader(tmp_path, BMAD_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _write_md(tmp_path, "spine4/ARCHITECTURE-SPINE.md", text)
    report = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True, propose=True)
    assert report.imported == 4  # parent + 3 distinct children
    assert report.superseded == 0
    refs = sorted(d.provenance.ref for d in store.iter_decisions() if "#" in d.provenance.ref)
    assert len(refs) == len(set(refs)) == 3
    assert all(d.supersedes is None for d in store.iter_decisions())
    # the flip-flop pin: an unchanged re-import must be a pure no-op
    report2 = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True, propose=True)
    assert report2.skipped_existing == 4
    assert report2.superseded == 0 and report2.imported == 0


def test_split_intro_is_the_parent_choice_when_no_other_section_matches(tmp_path):
    """Code review F9: the split section's pre-H3 intro is a named step of the parent's
    choice chain; no test exercised it. A spine with intro prose, one child, and NO
    conventions section → parent choice == the intro. Red against dropping the
    split_intro chain step."""
    text = (
        "# Spine — intro\n\n"
        "## Design Paradigm\n\nHexagonal.\n\n"
        "## Invariants & Rules\n\n"
        "These invariants bind every module below; numbering is stable.\n\n"
        "### AD-1 — one\n\n- **Rule:** `IntakeQueue` is capped\n\n"
        "## Deferred\n\nlater.\n"
    )
    parsed = parse_decision_doc(text, "x/ARCHITECTURE-SPINE.md", dialect=BMAD_DIALECT)
    assert parsed is not None
    assert "numbering is stable" in parsed.choice
    assert "IntakeQueue" not in parsed.choice  # the child's content stayed with the child


def test_duplicate_heading_text_does_not_evict_unrelated_sections(tmp_path):
    """Code review F3: the exclusion is keyed on (heading, body) pairs, not heading text.
    Shape H — a child H3 named like a later real H2 must not knock the real section out
    of the parent's choice pool."""
    text = (
        "# Spine — dup names\n\n"
        "## Design Paradigm\n\nHex.\n\n"
        "## Invariants & Rules\n\n"
        "### Consistency Conventions\n\n- **Rule:** `IntakeQueue` child rule\n\n"
        "## Consistency Conventions\n\nmoney as integer minor units\n\n"
        "## Deferred\n\nlater.\n"
    )
    parsed = parse_decision_doc(text, "x/ARCHITECTURE-SPINE.md", dialect=BMAD_DIALECT)
    assert parsed is not None
    # the REAL conventions section survives as the parent's choice…
    assert "money as integer minor units" in parsed.choice
    # …and the parent did not degrade into choice == context (the measured F3 symptom)
    assert "Hex." not in parsed.choice


def test_dry_run_items_carry_ref_and_by_file_groups_by_document(tmp_path):
    # Code review F4: the `ref` key existed but nothing read or guarded it, and by_file()
    # had no doc-import test at all.
    reader = _reader(tmp_path, BMAD_GRAPH)
    store = Store(tmp_path / "s.db")
    doc = _write_md(tmp_path, "spine5/ARCHITECTURE-SPINE.md", _read("bmad/ARCHITECTURE-SPINE.md"))
    report = import_docs(store, reader, [str(doc)], profile="bmad", any_doc=True, dry_run=True)
    assert len(report.dry_run) == 3
    assert sorted(report.dry_run[0]) == ["action", "anchors_skipped", "file_path", "ref", "title"]
    refs = {item["ref"] for item in report.dry_run}
    assert str(doc) in refs
    assert sum(1 for r in refs if "#ad-" in r) == 2
    assert all(item["file_path"] == str(doc) for item in report.dry_run)
    assert report.by_file() == {str(doc): 3}
    assert len(list(store.iter_decisions())) == 0  # dry run wrote nothing


def test_bmad_dialect_pins_split_and_deferred():
    # Spec T14: the tuples themselves, so a silent regression is caught even where no
    # fixture exercises the changed keyword (same standard as spec-kit's consequences pin).
    assert BMAD_DIALECT.split_choice == ("invariants & rules",)
    assert BMAD_DIALECT.consequences == ("deferred",)
    assert BMAD_DIALECT.rejected == ()
    # every other shipped profile keeps the default — the T11 boundary in tuple form
    for name in ("generic-adr", "superpowers", "genkovich-sdd", "spec-kit"):
        assert get_profile(name).dialect.split_choice == ()


def test_bmad_globs_find_spine_only_not_prd_or_memlog():
    # Upstream layout (measured 2026-07-30 at bmad-code-org/BMAD-METHOD 9b672e1e):
    # {project-root}/_bmad-output/planning-artifacts/<kind>/<kind>-<project>-<date>/ —
    # spine_output_path = "{planning_artifacts}/architecture", run_folder_pattern =
    # "architecture-{project_name}-{date}". Exact-set assertion, not membership.
    found = _glob_discovers("bmad", "bmad/tree")
    assert found == {
        "_bmad-output/planning-artifacts/architecture/"
        "architecture-payments-2026-07-30/ARCHITECTURE-SPINE.md"
    }
    # prd.md sits right there on disk and is deliberately NOT discovered (R3): BMAD's PRD
    # is a requirements document (Vision / Features / Non-Goals / MVP Scope / Success
    # Metrics) with no heading any `choice` keyword can match — same exclusion as
    # genkovich's and spec-kit's spec.md.
    assert "_bmad-output/planning-artifacts/prds/prd-payments-2026-07-30/prd.md" not in found
    # .memlog.md sits in the SAME run folder as the spine and is deliberately NOT
    # discovered (owner ruling 2026-07-30, variant A): it is a flat, headingless
    # append-only log the heading-dialect reader would only degrade into a degenerate
    # record. If a future edit adds it to ingest_globs, this line makes the regression
    # and its reason legible.
    assert (
        "_bmad-output/planning-artifacts/architecture/"
        "architecture-payments-2026-07-30/.memlog.md" not in found
    )


def test_registry_has_every_shipped_profile():
    # Spec T1: openspec joins the registry (design/superpowers/specs/2026-08-06-openspec-
    # profile-design.md).
    assert set(PROFILES) == {
        "generic-adr",
        "superpowers",
        "genkovich-sdd",
        "spec-kit",
        "bmad",
        "openspec",
    }
    assert isinstance(PROFILES["genkovich-sdd"], FlowProfile)
    assert isinstance(PROFILES["spec-kit"], FlowProfile)
    assert isinstance(PROFILES["bmad"], FlowProfile)
    assert isinstance(PROFILES["openspec"], FlowProfile)


# -- I2 (R1 improvement wave §2): FlowProfile.in_flight_note + its construction-time gate --


def test_openspec_profile_carries_the_in_flight_note():
    profile = get_profile("openspec")
    assert profile.in_flight_note is not None
    assert "in flight" in profile.in_flight_note.lower()


def test_in_flight_note_requires_ref_normalize():
    """T-I2e: setting in_flight_note without ref_normalize is a config error at
    profile-construction time (review Minor 6 — silent no-op forbidden). Red against a
    silent-no-op implementation (no validator at all)."""
    with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError wraps ValueError
        FlowProfile(
            name="bad",
            dialect=GENERIC_ADR_DIALECT,
            ingest_globs=("docs/*.md",),
            in_flight_note="note",
        )


def test_in_flight_note_with_ref_normalize_constructs_cleanly():
    """Declared-exception-adjacent: guards against the validator over-firing on the
    legitimate combination."""
    profile = FlowProfile(
        name="ok",
        dialect=GENERIC_ADR_DIALECT,
        ingest_globs=("docs/*.md",),
        ref_normalize=(r"docs/archive/", "docs/"),
        in_flight_note="note",
    )
    assert profile.in_flight_note == "note"


def test_get_profile_returns_named_profile():
    assert get_profile("superpowers").dialect is SUPERPOWERS_DIALECT
    # Closes the same gap for genkovich-sdd (pre-existing) and spec-kit (this task): every
    # other spec-kit/genkovich assertion passes a dialect straight to parse_decision_doc, so
    # none of them would catch a copy-paste that wires the registry entry to the wrong
    # dialect (e.g. GENERIC_ADR_DIALECT) — the suite would stay green while import_docs
    # silently ingested nothing on a real repo.
    assert get_profile("genkovich-sdd").dialect is GENKOVICH_SDD_DIALECT
    assert get_profile("spec-kit").dialect is SPEC_KIT_DIALECT
    assert get_profile("bmad").dialect is BMAD_DIALECT
    # Spec T1 (design note §4): the same identity check for openspec.
    assert get_profile("openspec").dialect is OPENSPEC_DIALECT


def test_get_profile_unknown_raises_valueerror_listing_valid():
    with pytest.raises(ValueError, match="unknown flow profile 'nope'"):
        get_profile("nope")


def test_generic_adr_dialect_matches_todays_doc_import_constants():
    # The lift must be byte-identical to the current module constants — snapshot them here
    # so a copy typo fails loudly (the behaviour-preserving-default guarantee).
    assert GENERIC_ADR_DIALECT.context == ("context", "trigger", "root cause")
    assert GENERIC_ADR_DIALECT.choice == ("decision outcome", "decisions", "decision", "design")
    assert GENERIC_ADR_DIALECT.rejected == (
        "rejected",
        "alternatives",
        "considered alternatives",
        "considered options",
        "options considered",
        # Added 2026-08-03, calibrated on two foreign corpora — see the tuple's own comment
        # and test_doc_import's PROPOSED_ALTERNATIVES_FIXTURE / OPTIONS_HEADING_FIXTURE.
        "proposed alternatives",
        "options",
    )
    assert GENERIC_ADR_DIALECT.consequences == ("consequences",)
    assert GENERIC_ADR_DIALECT.gate_only == ("status", "residuals", "scope notes", "user decisions")


def test_all_headings_concatenates_in_order():
    d = ReaderDialect(
        context=("a",), choice=("b",), rejected=("c",), consequences=("d",), gate_only=("e",)
    )
    assert d.all_headings() == ("a", "b", "c", "d", "e")


def test_superpowers_ingests_specs_only_not_plans():
    assert PROFILES["superpowers"].ingest_globs == ("docs/superpowers/specs/*.md",)


def test_generic_adr_fixture_parses_under_default_dialect():
    parsed = parse_decision_doc(_read("adr/0001-use-sessions.md"), "docs/adr/0001-use-sessions.md")
    assert parsed is not None
    assert parsed.title == "Use server-side sessions for auth"
    assert "keep users logged in" in parsed.context
    assert "server-side sessions" in parsed.choice
    assert parsed.consequences is not None and "evicted on logout" in parsed.consequences
    assert parsed.rejected is not None and "JWTs in localStorage" in parsed.rejected


def test_superpowers_fixture_parses_only_under_superpowers_dialect():
    text = _read("superpowers/2026-07-01-example-design.md")
    rel = "docs/superpowers/specs/2026-07-01-example-design.md"
    # default (generic-adr) dialect: Goal/Architecture/Approaches/Risks are unknown → skipped.
    assert parse_decision_doc(text, rel) is None
    # superpowers dialect: the free-form headings map onto the decision fields.
    parsed = parse_decision_doc(text, rel, dialect=SUPERPOWERS_DIALECT)
    assert parsed is not None
    assert parsed.title == "Retry policy for the payments worker"
    assert "double-charging" in parsed.context  # Goal → context
    assert "idempotency key" in parsed.choice  # Architecture → choice
    assert parsed.rejected is not None and "Distributed locks" in parsed.rejected  # Approaches
    assert parsed.consequences is not None and "at-least-once" in parsed.consequences  # Risks
    assert parsed.frontmatter_status == "draft"


def test_thin_superpowers_spec_has_no_rejected_or_consequences():
    parsed = parse_decision_doc(
        _read("superpowers/2026-07-01-thin-design.md"),
        "docs/superpowers/specs/2026-07-01-thin-design.md",
        dialect=SUPERPOWERS_DIALECT,
    )
    assert parsed is not None
    assert "TTL cache" in parsed.choice
    assert parsed.rejected is None  # honest: nothing to capture until Slice 2 injection
    assert parsed.consequences is None


SUPERPOWERS_GRAPH = {
    "nodes": [
        {
            "id": "file_pay",
            "label": "payments.py",
            "norm_label": "payments.py",
            "file_type": "code",
            "source_file": "payments.py",
            "community": 1,
        }
    ],
    "links": [],
}


def _reader(tmp_path, graph, name="g.json"):
    p = tmp_path / name
    p.write_text(json.dumps(graph))
    return GraphifyReader(p)


def _write_md(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_import_docs_superpowers_profile_lands_proposed_and_anchors(tmp_path):
    graph_path = tmp_path / "g.json"
    reader = _reader(tmp_path, SUPERPOWERS_GRAPH)
    graph_bytes_before = graph_path.read_bytes()
    store = Store(tmp_path / "s.db")
    doc = _write_md(
        tmp_path,
        "docs/superpowers/specs/x.md",
        _read("superpowers/2026-07-01-example-design.md"),
    )

    # any_doc=True: the doc's path is absolute (tmp_path-based, not under the real cwd),
    # so D7.1's glob check (relative to cwd, same convention BUG F's anchor-lookup
    # normalization already documents) would otherwise misread it as outside-profile —
    # this test is about profile-driven anchoring/proposal behavior, not glob enforcement.
    report = import_docs(store, reader, [str(doc)], profile="superpowers", any_doc=True)

    assert report.imported == 1
    assert report.status_derived_proposed == 1  # status: draft forces proposed
    d = next(store.iter_decisions())
    assert d.status is DecisionStatus.PROPOSED
    assert "idempotency key" in d.choice
    # anchored to the `payments.py` path mention → a tier-2 leaf binding
    assert any(b.tier == 2 for b in store.bindings_for_record(d.id))
    # graph.json is read-only input — untouched by the import
    assert graph_path.read_bytes() == graph_bytes_before


def test_import_docs_unknown_profile_raises_valueerror(tmp_path):
    reader = _reader(tmp_path, SUPERPOWERS_GRAPH)
    store = Store(tmp_path / "s.db")
    with pytest.raises(ValueError, match="unknown flow profile"):
        import_docs(store, reader, [], profile="nope")
