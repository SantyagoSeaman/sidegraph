"""md1: section resolution over markdown-it-py block tokens."""

import hashlib
import random
import time

import pytest

from sidegraph import md_sections
from sidegraph.md_sections import (
    SectionResolveError,
    Unsupported,
    compare_entry,
    parse_md1,
    resolve_suffix,
    section_text_hash,
)
from sidegraph.schema import DeclaredSection, DeclaredSectionFile

ZERO = b"\0" * 32


def chains(doc) -> list[tuple[str, ...]]:
    return [h.chain for h in doc.headings]


def span_of(doc, *suffix: str) -> tuple[int, int]:
    h = resolve_suffix(doc, suffix)
    return h.first_line, h.end_line


# --- spans and chains ---------------------------------------------------------------------


def test_atx_setext_and_indented_atx_headings_form_chains() -> None:
    text = "# Top\n\n## A\nbody\n\n  ## Indented\n\nSetext\n------\n\n### Deep ###\n"
    doc = parse_md1(text)
    assert chains(doc) == [
        ("Top",),
        ("Top", "A"),
        ("Top", "Indented"),
        ("Top", "Setext"),
        ("Top", "Setext", "Deep"),
    ]
    assert [(h.first_line, h.end_line) for h in doc.headings] == [
        (0, 12),
        (2, 5),
        (5, 7),
        (7, 12),
        (10, 12),
    ]
    assert doc.total_lines == 11


def test_span_includes_subsections_ends_at_equal_or_higher_heading() -> None:
    doc = parse_md1("# T\n## A\na\n### A1\nb\n## B\nc\n")
    assert span_of(doc, "A") == (1, 5)
    assert span_of(doc, "A1") == (3, 5)
    assert span_of(doc, "B") == (5, 8)
    assert span_of(doc, "T") == (0, 8)


def test_text_before_first_heading_not_addressable() -> None:
    doc = parse_md1("intro\n\n# T\nx\n")
    assert chains(doc) == [("T",)]
    assert doc.headings[0].first_line == 2
    with pytest.raises(SectionResolveError) as err:
        resolve_suffix(doc, ["intro"])
    assert err.value.code == "unknown_section"


def test_resolve_suffix_unique_unknown_ambiguous() -> None:
    doc = parse_md1("# T\n## A\n### Rules\n## B\n### Rules\n## C\n### Rules\n")
    assert resolve_suffix(doc, ["A", "Rules"]).chain == ("T", "A", "Rules")
    assert resolve_suffix(doc, ["T", "C", "Rules"]).chain == ("T", "C", "Rules")
    with pytest.raises(SectionResolveError) as err:
        resolve_suffix(doc, ["Rules"])
    assert err.value.code == "ambiguous_section"
    with pytest.raises(SectionResolveError) as err:
        resolve_suffix(doc, ["Nope"])
    assert err.value.code == "unknown_section"


# --- containment --------------------------------------------------------------------------


def test_html_blocks_with_hash_lines_do_not_cut() -> None:
    text = (
        "# T\n## Rules\n<div>\n# fake in div\n</div>\n\n"
        "<!--\n# fake in comment\n\n# fake after blank\n-->\n\n"
        "<pre>\n# p\n\n# q\n</pre>\n## After\n"
    )
    doc = parse_md1(text)
    assert chains(doc) == [("T",), ("T", "Rules"), ("T", "After")]
    after = resolve_suffix(doc, ["After"])
    assert span_of(doc, "Rules")[1] == after.first_line


def test_fences_backtick_tilde_longer_closer_not_headings() -> None:
    text = (
        "# T\n## A\n```\n# in backtick\n```\n~~~\n# in tilde\n~~~~~\n"
        "````\n# longer\n```\n# still inside\n`````\n## B\n"
    )
    assert chains(parse_md1(text)) == [("T",), ("T", "A"), ("T", "B")]


OPUS_H1 = (
    "# T\n## Rules\n1. item\n    ```\n    a\n   ```\n  ```\n# phantom\n  ```\n"
    " - item\n     ```\n     b\n   ```\nThe rule: X is allowed.\n"
)


def test_opus_h1_document_fence_in_list_item_mixed_indents() -> None:
    doc = parse_md1(OPUS_H1)
    assert chains(doc) == [("T",), ("T", "Rules")]
    edited = parse_md1(OPUS_H1.replace("allowed", "FORBIDDEN"))
    rules = resolve_suffix(doc, ["Rules"])
    assert section_text_hash(doc, rules) != section_text_hash(
        edited, resolve_suffix(edited, ["Rules"])
    )


def test_u2028_and_formfeed_do_not_split_lines() -> None:
    text = "# T\n## A\nx # y\x0c# z # w\u0085# v\n## B\n"
    doc = parse_md1(text)
    assert chains(doc) == [("T",), ("T", "A"), ("T", "B")]
    assert len(doc.lines) == 5


# --- hashing ------------------------------------------------------------------------------


def _last_text_hash(text: str) -> str:
    doc = parse_md1(text)
    return section_text_hash(doc, doc.headings[-1])


def test_final_newline_removal_changes_last_text_hash() -> None:
    assert _last_text_hash("# T\n## A\nbody\n") != _last_text_hash("# T\n## A\nbody")


def test_blank_line_inside_fence_and_hard_break_change_text_hash() -> None:
    base = "# T\n## A\n```py\nx = '''a\nb'''\n```\n"
    with_blank = "# T\n## A\n```py\nx = '''a\n\nb'''\n```\n"
    assert _last_text_hash(base) != _last_text_hash(with_blank)
    assert _last_text_hash("# T\n## A\nline\nbreak\n") != _last_text_hash(
        "# T\n## A\nline  \nbreak\n"
    )


def test_crlf_and_cr_conversion_keep_all_hashes() -> None:
    text = "# T\n## A\nbody\n\n[r]: http://x\n## B\nmore\n"
    base = parse_md1(text)
    for variant in (text.replace("\n", "\r\n"), text.replace("\n", "\r")):
        other = parse_md1(variant)
        assert [h.hash for h in other.headings] == [h.hash for h in base.headings]
        assert [section_text_hash(other, h) for h in other.headings] == [
            section_text_hash(base, h) for h in base.headings
        ]
        assert other.refs_hash == base.refs_hash


def test_bom_stays_in_line_one() -> None:
    doc = parse_md1("﻿# T\n## A\nx\n")
    assert doc.lines[0].startswith("﻿")
    assert chains(doc) == [("A",)]  # the BOM makes line 1 a paragraph, not a heading


def test_heading_hash_is_incremental_chain_hash() -> None:
    doc = parse_md1("# Top\n## Mid\n### Leaf\n")
    parent = ZERO
    for heading, text in zip(doc.headings, ("Top", "Mid", "Leaf"), strict=True):
        segment = hashlib.sha256(text.encode()).digest()
        parent = hashlib.sha256(b"md1\0" + parent + segment).digest()
        assert heading.hash == parent.hex()


def test_long_ancestor_h1_with_3000_sections_is_linear() -> None:
    title = "T" * 1_900_000
    text = f"# {title}\n" + "".join(f"## s{i}\n" for i in range(3000))
    start = time.perf_counter()
    doc = parse_md1(text)
    assert len(doc.headings) == 3001
    assert time.perf_counter() - start < 2.0


# --- gates --------------------------------------------------------------------------------


def _gate(text: str, **kw) -> str:
    with pytest.raises(Unsupported) as err:
        parse_md1(text, **kw)
    return err.value.gate


def test_line_cap_refused_before_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    import markdown_it

    def boom(*a, **k):
        raise AssertionError("parser constructed")

    monkeypatch.setattr(markdown_it, "MarkdownIt", boom)
    assert _gate("x\n" * 20_001) == "lines"
    assert md_sections.count_lines("x\n" * 20_000) == 20_000
    assert md_sections.count_lines("x\n" * 20_000 + "y") == 20_001


def test_twenty_thousand_lines_with_final_newline_accepted() -> None:
    doc = parse_md1("# T\n" + "x\n" * 19_999)
    assert doc.total_lines == 20_000


def test_heading_cap_5001() -> None:
    assert _gate("".join(f"# h{i}\n" for i in range(5001))) == "headings"
    assert len(parse_md1("".join(f"# h{i}\n" for i in range(5000))).headings) == 5000


def test_token_cap_refused_by_token_gate() -> None:
    from markdown_it import MarkdownIt

    # Measured with markdown-it-py 4.2.0: "- > " x 3 per line over 20,000 lines gives
    # 320,002 tokens at a maximum level of 8, so only the token gate can refuse it.
    text = "\n".join("- > " * 3 for _ in range(20_000))
    tokens = MarkdownIt("commonmark").disable("inline").parse(text, {})
    assert len(tokens) > 200_000
    assert max(t.level for t in tokens) < 19
    assert _gate(text) == "tokens"


def test_nesting_gate_boundary() -> None:
    empty = parse_md1("x\n").refs_hash
    for depth in (18, 19):
        doc = parse_md1("> " * depth + "[a]: http://x\n")
        assert doc.refs_hash != empty
    assert _gate("> " * 20 + "[a]: http://x\n") == "nesting"


def test_heading_inside_list_item_and_blockquote_unsupported() -> None:
    assert _gate("# T\n- ## in list\n") == "container"
    assert _gate("# T\n> ## in quote\n") == "container"


def test_parser_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from markdown_it import MarkdownIt

    def boom(self, *a, **k):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(MarkdownIt, "parse", boom)
    assert _gate("# T\n") == "parse"


def test_remaining_exhausted_between_heading_batches() -> None:
    text = "".join(f"# h{i}\n" for i in range(600))
    values = iter([1.0, -1.0])
    assert _gate(text, remaining=lambda: next(values)) == "budget"
    assert len(parse_md1(text, remaining=lambda: None).headings) == 600


# --- parser canary ------------------------------------------------------------------------


def test_parser_canary_pins_chain_lines_and_references() -> None:
    text = "# A\n\n## B\n  ## C\nD\n--\n\n### E ###\n[x]: /u 'T'\n\n```\n# no\n```\n"
    doc = parse_md1(text)
    got = [(h.chain, h.first_line, h.end_line) for h in doc.headings]
    assert got == [
        (("A",), 0, 14),
        (("A", "B"), 2, 3),
        (("A", "C"), 3, 4),
        (("A", "D"), 4, 14),
        (("A", "D", "E"), 7, 14),
    ]
    refs = [["X", "/u", "T"]]  # labels are case-folded by the parser
    import json

    assert (
        doc.refs_hash
        == hashlib.sha256(
            json.dumps(refs, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )
    # budget boundaries stay exactly where the spec puts them
    assert len(parse_md1("# T\n" + "x\n" * 19_999).lines) == 20_001
    assert len(parse_md1("".join(f"# h{i}\n" for i in range(5000))).headings) == 5000
    # nesting boundary: depth 19 is read, depth 20 is refused
    assert parse_md1("> " * 19 + "[a]: /x\n").refs_hash != parse_md1("x\n").refs_hash
    assert _gate("> " * 20 + "[a]: /x\n") == "nesting"


# --- comparison ---------------------------------------------------------------------------


def entry_for(text: str, declared: list[list[str]], watch: list[list[str]] | None = None):
    doc = parse_md1(text)
    sections = []
    for role, group in (("declared", declared), ("watch", watch or [])):
        for suffix in group:
            heading = resolve_suffix(doc, suffix)
            sections.append(
                DeclaredSection(
                    heading_hash=heading.hash,
                    role=role,
                    text_hash=section_text_hash(doc, heading),
                )
            )
    return DeclaredSectionFile(
        path="spec.md", scheme="md1", refs_hash=doc.refs_hash, sections=sections
    )


def entry_of(text: str, headings) -> DeclaredSectionFile:
    """An entry declaring exactly these headings (no suffix lookup)."""
    doc = parse_md1(text)
    sections = [
        DeclaredSection(heading_hash=h.hash, role="declared", text_hash=section_text_hash(doc, h))
        for h in headings
    ]
    return DeclaredSectionFile(
        path="spec.md", scheme="md1", refs_hash=doc.refs_hash, sections=sections
    )


def causes(entry, text: str) -> list[str]:
    return compare_entry(entry, parse_md1(text))


DOC = (
    "# Spec\n\n## 30. Other\nx\n\n[r]: http://a\n\n## 55. Vocab\n\n### 55.2 Rules\nrule one\n\n"
    "## 56. Echo\n\n### 56.4 Amend\nsib\n"
)
RULES = [["55.2 Rules"]]
AMEND = [["56.4 Amend"]]


def test_edit_outside_declared_is_unchanged() -> None:
    entry = entry_for(DOC, RULES, AMEND)
    assert causes(entry, DOC.replace("x\n", "xx\n", 1)) == []


def test_declared_edit_rearms() -> None:
    entry = entry_for(DOC, RULES)
    assert causes(entry, DOC.replace("rule one", "rule two")) == ["declared_changed"]
    assert causes(entry, DOC.replace("rule one", "rule one  ")) == ["declared_changed"]


def test_edit_on_last_line_of_declared_span_rearms() -> None:
    text = "# T\n## Rules\nfirst\nlast\n## Next\nz\n"
    entry = entry_for(text, [["Rules"]])
    assert causes(entry, text.replace("last", "LAST")) == ["declared_changed"]


def test_watch_edit_rearms() -> None:
    entry = entry_for(DOC, RULES, AMEND)
    assert causes(entry, DOC.replace("sib", "sib2")) == ["watch_changed"]


def test_sibling_edit_without_watch_clean() -> None:
    assert causes(entry_for(DOC, RULES), DOC.replace("sib", "sib2")) == []


def test_rename_remove_true_duplicate_chain_rearm() -> None:
    entry = entry_for(DOC, RULES)
    assert "declared_not_found" in causes(entry, DOC.replace("55.2 Rules", "55.2 Laws"))
    assert "declared_not_found" in causes(entry, DOC.replace("### 55.2 Rules\nrule one\n", ""))
    dup = DOC + "\n## 55. Vocab\n\n### 55.2 Rules\n"
    assert "declared_not_found" in causes(entry, dup)
    watched = entry_for(DOC, RULES, AMEND)
    assert "watch_changed" in causes(watched, DOC.replace("56.4 Amend", "56.4 Other"))


@pytest.mark.parametrize(
    "ancestor, renamed",
    [
        ("## Chapter", "## Chapters"),
        ("  ## Chapter", "  ## Chapters"),
        ("Chapter\n-------", "Chapters\n-------"),
    ],
)
def test_ancestor_rename_rearms(ancestor: str, renamed: str) -> None:
    text = f"# T\n{ancestor}\n### Rules\nx\n"
    entry = entry_for(text, [["Rules"]])
    assert causes(entry, text.replace(ancestor, renamed)) == ["declared_not_found"]
    other = f"# T\n## Elsewhere\ny\n{ancestor}\n### Rules\nx\n"
    entry = entry_for(other, [["Rules"]])
    assert causes(entry, other.replace("y\n", "Underlined\n---\ny\n", 1)) == []


@pytest.mark.parametrize(
    "before, after",
    [
        ("", "[r]: http://a\n"),
        ("[r]: http://a\n", "[r]: http://b\n"),
        ("[r]: http://a\n", ""),
        ("> [r]: https://old\n", "> [r]: https://new\n"),
        ("- [r]: https://old\n", "- [r]: https://new\n"),
        ("[multi\nword]: https://old\n", "[multi\nword]: https://new\n"),
        ("[m]: /first\n\n[m]: /second\n", "[m]: /second\n"),
    ],
)
def test_reference_table_change_rearms(before: str, after: str) -> None:
    base = "# P\n## Policy\nSee [r] and [multi word] and [m].\n## Other\n{}"
    entry = entry_for(base.format(before), [["Policy"]])
    assert causes(entry, base.format(after)) == ["refs_changed"]


def test_definition_line_inside_paragraph_is_not_a_definition() -> None:
    base = "# P\n## Policy\nSee [m].\n## Other\npara\n[m]: /{}\n"
    entry = entry_for(base.format("x"), [["Policy"]])
    assert causes(entry, base.format("y")) == []


def test_causes_follow_spec_order() -> None:
    entry = entry_for(DOC, RULES, AMEND)
    text = DOC.replace("rule one", "R").replace("http://a", "http://b").replace("sib", "S")
    assert causes(entry, text) == ["declared_changed", "refs_changed", "watch_changed"]
    gone = text.replace("55.2 Rules", "55.2 Laws")
    assert causes(entry, gone)[0] == "declared_not_found"


# --- oracle -------------------------------------------------------------------------------

FRAGMENTS = [
    "# T\n", "## A\n", "## B\n", "### A\n", "### Rules\n", "#### Deep\n", "  ## Indented\n",
    "   # Indented1\n", "Setext\n---\n", "Setext1\n===\n", "para one\n", "para two\n",
    "See [r] and [multi\nword].\n", "[r]: https://old.example\n", "[r]: https://new.example\n",
    "[multi word]: /m1\n", "[m]: is how we say it\n", "> quote\n", "> [r]: https://q.example\n",
    "> ## quoted heading\n", "- item\n", "- [r]: https://l.example\n", "- # list heading\n",
    "1. item\n    ```\n    a\n   ```\n", "  ```\n# in fence\n  ```\n", "```\n# fenced\n```\n",
    "~~~\n## tilde\n~~~\n", "````\n```\n# in long fence\n````\n", "<pre>\n# in pre\n</pre>\n",
    "<div>\n\n# after blank in div\n</div>\n", "<!--\n# in comment\n\n## still comment\n-->\n",
    "   <div>\n# x\n", "<!x\n# y\n", "    code line\n", "    # indented code\n", "\n", "\n",
    "---\n", "***\n", "| a | b |\n|---|---|\n| 1 | 2 |\n", "text with trailing  \nbreak\n",
    "[^1] note\n", "a # not heading\n", "\x0c# ff\n",
]  # fmt: skip


def _random_doc(rng: random.Random) -> str:
    return "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(4, 16)))


def _oracle(full, text: str) -> dict[tuple[str, ...], str | None]:
    """Rendered HTML per top-level section, from a second inline-enabled parse."""
    env: dict = {}
    toks = full.parse(text, env)
    heads = [(i, t) for i, t in enumerate(toks) if t.type == "heading_open" and t.level == 0]
    out: dict[tuple[str, ...], str | None] = {}
    stack: list[tuple[int, str]] = []
    for k, (i, tok) in enumerate(heads):
        level = int(tok.tag[1])
        title = md_sections._normalize_heading(toks[i + 1].content)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        chain = tuple(x[1] for x in stack)
        end = next((j for j, t in heads[k + 1 :] if int(t.tag[1]) <= level), len(toks))
        html = full.renderer.render(toks[i:end], full.options, env)
        out[chain] = None if chain in out else html
    return out


def _edit(rng: random.Random, v1: str) -> str:
    lines = v1.split("\n")
    mode = rng.choice(["frag", "frag", "line", "del"])
    if mode == "frag":
        if rng.random() < 0.3:
            return v1 + rng.choice(FRAGMENTS)
        return v1.replace(rng.choice(FRAGMENTS), rng.choice(FRAGMENTS), 1)
    i = rng.randrange(len(lines))
    if mode == "line":
        lines[i] = rng.choice(
            ["x", "# H", "[r]: https://z.example", "```", "> q", "", lines[i] + " "]
        )
    else:
        del lines[i]
    return "\n".join(lines)


def test_oracle_declared_html_change_never_clean() -> None:
    from markdown_it import MarkdownIt

    full = MarkdownIt("commonmark")
    rng = random.Random(417)
    pairs = 0
    while pairs < 600:
        v1 = _random_doc(rng)
        try:
            doc = parse_md1(v1)
        except Unsupported:
            continue
        unique = [h for h in doc.headings if sum(g.chain == h.chain for g in doc.headings) == 1]
        if not unique:
            continue
        declared = rng.sample(unique, min(len(unique), rng.randint(1, 2)))
        v2 = _edit(rng, v1)
        if v2 == v1:
            continue
        try:
            parsed2 = parse_md1(v2)
        except Unsupported:
            pairs += 1  # the scan falls back to the whole file: changed by construction
            continue
        pairs += 1
        entry = entry_of(v1, declared)
        o1, o2 = _oracle(full, v1), _oracle(full, v2)
        html_changed = any(
            o1.get(h.chain) is None or o2.get(h.chain) is None or o1[h.chain] != o2[h.chain]
            for h in declared
        )
        if html_changed:
            assert compare_entry(entry, parsed2) != [], (v1, v2, [h.chain for h in declared])


def test_random_edits_rearm_subset_of_whole_file() -> None:
    rng = random.Random(12)
    checked = 0
    while checked < 200:
        v1 = _random_doc(rng)
        try:
            doc = parse_md1(v1)
        except Unsupported:
            continue
        unique = [h for h in doc.headings if sum(g.chain == h.chain for g in doc.headings) == 1]
        if not unique:
            continue
        entry = entry_of(v1, unique[:1])
        checked += 1
        # the same text in other line endings is a different blob but never a re-arm
        for variant in (v1, v1.replace("\n", "\r\n"), v1.replace("\n", "\r")):
            assert compare_entry(entry, parse_md1(variant)) == []


# --- footnote scan -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "span, expected",
    [
        ("[^1]", True),
        ("see [^a-z] here", True),
        ("[^[^a]", True),
        ("[^]", False),
        ("[^ a]", False),
        ("[^a b]", False),
        ("x[^", False),
        ("[^abc", False),
        ("[ ^a]", False),
        ("[^ a] then [^b]", True),
    ],
)
def test_footnote_ref_matches_the_documented_pattern(span: str, expected: bool) -> None:
    doc = parse_md1(f"# T\n{span}\n")
    assert md_sections.footnote_ref(doc, doc.headings[0]) is expected


def test_footnote_scan_is_linear_on_overlapping_openers() -> None:
    doc = parse_md1("# T\n" + "[^" * 64_000)
    start = time.perf_counter()
    assert md_sections.footnote_ref(doc, doc.headings[0]) is False
    assert time.perf_counter() - start < 1.0
