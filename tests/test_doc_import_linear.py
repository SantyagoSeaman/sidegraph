"""The doc parser's structural work is linear in the heading count, with identical output.

``tests/fixtures/doc_import_parse_golden.json`` freezes the output of the parser as it was
before the linear-time change (one SHA-256 per generated document). It was produced once, on the
unfixed code, with:

    uv run python -c "import json; from tests.test_doc_import_linear import golden_digests; \
json.dump({'seed': 20261107, 'count': 300, 'digests': golden_digests()}, \
open('tests/fixtures/doc_import_parse_golden.json', 'w'), indent=0)"

Never regenerate it after that: it is the old algorithm, kept as data.

# see design/superpowers/specs/2026-11-07-doc-import-linear-time-design.md
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

from sidegraph.doc_import import parse_decision_docs
from sidegraph.profiles import get_profile

GOLDEN = Path(__file__).parent / "fixtures" / "doc_import_parse_golden.json"
SEED = 20261107
COUNT = 300
DIALECTS = ("generic-adr", "openspec", "bmad")
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz123456"
TITLES = [
    "Same",
    "Same 2",
    "same-2",
    "Same-2-2",
    "Decision",
    "Decisions",
    "Context",
    "Alternatives",
    "",
    "Ünïcode",
    "x",
    "Invariants & Rules",
    f"Rotate {SECRET} now",
]
FLAT_PREFIX = "# Probe\n## Decision\nWe choose a bounded parser.\n"
SPLIT_PREFIX = "# Probe\n## Context\nShared context.\n## Decisions\nWe choose these rules.\n"


def _document(rng: random.Random, force_split: bool) -> str:
    lines: list[str] = []
    if rng.random() < 0.9:
        lines.append(f"# {rng.choice(['Probe', 'Title', 'Same'])}")
    if force_split:
        lines += ["", "## Context", "Shared context.", "", "## Invariants & Rules", "intro text"]
        lines += ["## Decisions", "We choose these rules."]
    for _ in range(rng.randint(0, 120)):
        level = rng.choices([1, 2, 3], weights=[1, 6, 6])[0]
        title = rng.choice(TITLES)
        lines.append(f"{'#' * level} {title}".rstrip() if title else "#" * level + " ")
        for _ in range(rng.randint(0, 3)):
            roll = rng.random()
            if roll < 0.15:
                lines += ["```", "# not a heading", "## nor this", "```"]
            elif roll < 0.25:
                lines.append("#### deeper than a heading")
            else:
                lines.append(f"body line {rng.randint(0, 9)}")
    return "\n".join(lines) + "\n"


def generate_corpus(seed: int, count: int) -> list[tuple[str, str]]:
    """``(dialect_name, markdown)`` pairs, deterministic for a seed."""
    rng = random.Random(seed)
    return [
        (DIALECTS[i % len(DIALECTS)], _document(rng, force_split=i % 5 == 0)) for i in range(count)
    ]


def digest(docs, reason) -> str:
    payload = {"docs": [d.model_dump(mode="json") for d in docs], "reason": reason}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def golden_digests() -> list[str]:
    out = []
    for dialect, text in generate_corpus(SEED, COUNT):
        docs, reason = parse_decision_docs(
            text, "docs/adr/probe.md", dialect=get_profile(dialect).dialect
        )
        out.append(digest(docs, reason))
    return out


def test_parse_output_is_unchanged_from_the_frozen_golden():
    frozen = json.loads(GOLDEN.read_text())
    assert frozen["seed"] == SEED and frozen["count"] == COUNT
    corpus = generate_corpus(SEED, COUNT)
    # the corpus must reach the split path and the fragment suffixes, or the golden guards less
    split_docs: dict[str, int] = {}
    for (dialect, text), expected in zip(corpus, frozen["digests"], strict=True):
        docs, reason = parse_decision_docs(
            text, "docs/adr/probe.md", dialect=get_profile(dialect).dialect
        )
        assert digest(docs, reason) == expected
        if any(d.fragment for d in docs):
            split_docs[dialect] = split_docs.get(dialect, 0) + 1
    assert sum(split_docs.values()) >= 10
    assert split_docs.get("openspec", 0) >= 1 and split_docs.get("bmad", 0) >= 1


# -- counting instruments ----------------------------------------------------------------


class CountingList(list):
    """Counts every element read, slice copies included; the built-in reverse iterator
    bypasses ``__getitem__`` and ``__iter__``, so ``__reversed__`` reads through counted
    integer access."""

    reads = 0

    def __getitem__(self, index):
        if isinstance(index, slice):
            self.reads += len(range(*index.indices(len(self))))
        else:
            self.reads += 1
        return list.__getitem__(self, index)

    def __iter__(self):
        for i in range(len(self)):
            self.reads += 1
            yield list.__getitem__(self, i)

    def __reversed__(self):
        for i in range(len(self) - 1, -1, -1):
            self.reads += 1
            yield list.__getitem__(self, i)


class CountingSet(set):
    checks = 0

    def __contains__(self, item):
        self.checks += 1
        return super().__contains__(item)


def _flat(n: int) -> CountingList:
    return CountingList((i + 3, 2, "X") for i in range(n))


def _reference_section_ends(rest, n_lines):
    """Today's loop, frozen from the pre-change parser (reference only)."""
    ends = []
    for idx, (_pos, lvl, _text) in enumerate(rest):
        body_end = n_lines
        for later_pos, later_lvl, _ in rest[idx + 1 :]:
            if later_lvl <= lvl:
                body_end = later_pos
                break
        ends.append(body_end)
    return ends


def _reference_child_spans(after, section_end, n_lines):
    """Today's child loop, frozen from the pre-change parser (reference only)."""
    out = []
    for pos, (i, lvl, text) in enumerate(after):
        if i >= section_end or lvl != 3:
            continue
        body_end = next((j for j, _, _ in after[pos + 1 :] if j > i), n_lines)
        out.append((text, i + 1, min(body_end, section_end)))
    return out


def _reference_fragment(base, used):
    fragment, k = base, 1
    while fragment in used:
        k += 1
        fragment = f"{base}-{k}"
    used.add(fragment)
    return fragment


N = 4096


def test_section_ends_reads_each_heading_a_bounded_number_of_times():
    from sidegraph.doc_import import _section_ends

    mixed = CountingList([(3, 2, "H2"), *((i + 4, 3, "H3") for i in range(N - 1))])
    for rest in (_flat(N), mixed):
        got = _section_ends(rest, N + 10)
        assert 0 < rest.reads <= 4 * N
        reference = type(rest)(list.__iter__(rest))
        assert got == _reference_section_ends(reference, N + 10)
        assert reference.reads > N * N // 4  # the old shape: documents the gap


def test_child_spans_reads_each_heading_a_bounded_number_of_times():
    from sidegraph.doc_import import _child_spans

    after = CountingList((i + 5, 3, f"Child {i}") for i in range(N))
    got = _child_spans(after, N + 100, N + 100)
    assert 0 < after.reads <= 4 * N
    reference = CountingList(list.__iter__(after))
    assert got == _reference_child_spans(reference, N + 100, N + 100)
    assert reference.reads > N * N // 4
    # containment guards: a child past the section end is skipped, and a body is clamped
    clamped = _child_spans([(5, 3, "in"), (9, 3, "past")], 8, 20)
    assert clamped == _reference_child_spans([(5, 3, "in"), (9, 3, "past")], 8, 20)


def test_next_fragment_checks_each_fragment_a_bounded_number_of_times():
    from sidegraph.doc_import import _next_fragment

    used, ordinals = CountingSet(), {}
    names = [_next_fragment("same", used, ordinals) for _ in range(N)]
    assert names == ["same"] + [f"same-{k}" for k in range(2, N + 1)]
    assert used.checks <= 2 * N
    reference = CountingSet()
    for _ in range(N):
        _reference_fragment("same", reference)
    assert reference.checks == N * (N + 1) // 2  # the old shape: documents the gap
    # a natural name that looks like an ordinal is still skipped, as before
    used, ordinals = set(), {}
    assert [_next_fragment("same", used, ordinals) for _ in range(3)] == [
        "same",
        "same-2",
        "same-3",
    ]
    used, ordinals = {"same-2"}, {}
    assert [_next_fragment("same", used, ordinals) for _ in range(3)] == [
        "same",
        "same-3",
        "same-4",
    ]
    used, ordinals = set(), {}
    got = [
        _next_fragment("same", used, ordinals),
        _next_fragment("same-2", used, ordinals),
        _next_fragment("same", used, ordinals),
        _next_fragment("same", used, ordinals),
    ]
    reference_used: set[str] = set()
    assert got == [
        _reference_fragment(b, reference_used) for b in ("same", "same-2", "same", "same")
    ]
    # a natural name above the stored pointer is skipped on resume, never reused
    used, ordinals = set(), {}
    got = [_next_fragment(b, used, ordinals) for b in ("same", "same", "same-3", "same")]
    assert got == ["same", "same-2", "same-3", "same-4"]


def test_next_fragment_matches_the_old_search_on_seeded_random_bases():
    from sidegraph.doc_import import _next_fragment

    bases = ["same", "same-2", "same-2-2", "same-3", "x", "x-2", "section-1"]
    rng = random.Random(SEED)
    used, ordinals, reference_used = set(), {}, set()
    deepest = 0
    for _ in range(2000):
        base = rng.choice(bases)
        got = _next_fragment(base, used, ordinals)
        assert got == _reference_fragment(base, reference_used)
        deepest = max(deepest, ordinals[base])
    # the golden corpus never takes a base past ordinal 2; this one resumes from deep pointers
    assert deepest >= 3


def test_next_fragment_skips_natural_names_when_it_resumes_from_a_stored_pointer():
    from sidegraph.doc_import import _next_fragment

    # short documents, so a natural name such as "same-4" arrives while "same" is still low
    bases = ["same", "same", "same-2", "same-3", "same-4", "same-5"]
    rng = random.Random(SEED)
    resumed_skips = 0
    for _ in range(400):
        used, ordinals, reference_used = set(), {}, set()
        for _ in range(8):
            base = rng.choice(bases)
            before = ordinals.get(base, 1)
            got = _next_fragment(base, used, ordinals)
            assert got == _reference_fragment(base, reference_used)
            if before > 1 and ordinals[base] > before + 1:
                resumed_skips += 1
    assert resumed_skips > 0


# -- scaling ------------------------------------------------------------------------------


def _best_of_three(text: str, dialect, rel_path: str = "docs/adr/probe.md") -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        parse_decision_docs(text, rel_path, dialect=dialect)
        best = min(best, time.perf_counter() - start)
    return best


def test_flat_h2_parse_time_scales_roughly_linearly():
    dialect = get_profile("generic-adr").dialect
    small = _best_of_three(FLAT_PREFIX + "## X\nx\n" * 8000, dialect)
    large = _best_of_three(FLAT_PREFIX + "## X\nx\n" * 32000, dialect)
    assert large / small < 8, (small, large)


def test_repeated_h3_parse_time_scales_roughly_linearly():
    dialect = get_profile("openspec").dialect
    path = "openspec/changes/probe/design.md"
    small = _best_of_three(SPLIT_PREFIX + "### Same\nx\n" * 1024, dialect, path)
    large = _best_of_three(SPLIT_PREFIX + "### Same\nx\n" * 4096, dialect, path)
    assert large / small < 8, (small, large)


def test_real_openspec_path_keeps_parent_and_child_refs(tmp_path, monkeypatch):
    from sidegraph.bootstrap.planner import plan_sources
    from sidegraph.bootstrap.scan import scan_sources
    from sidegraph.doc_import import import_docs
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store

    monkeypatch.chdir(tmp_path)
    profile = get_profile("openspec")
    rel = "openspec/changes/probe/design.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(SPLIT_PREFIX + "### Same\nx\n" * 512)
    plan = plan_sources(tmp_path, scan_sources(tmp_path, profile), profile)
    refs = [c.ref for c in plan.candidates]
    assert len(refs) == 513
    assert refs[:3] == [rel, f"{rel}#same", f"{rel}#same-2"]
    assert refs[-1] == f"{rel}#same-512"

    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    with Store(tmp_path / ".sidegraph") as store:
        report = import_docs(
            store, GraphifyReader(graph), [rel], profile="openspec", any_doc=True, dry_run=True
        )
    # the empty graph anchors nothing, so this checks the counts only; the refs above come
    # from the same parse that import_docs uses
    assert report.skipped_unanchorable == 513 and report.imported == 0
