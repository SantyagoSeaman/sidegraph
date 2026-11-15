"""The hard cap cuts the record lines of the decisions and the mistakes sections in the reverse
of the order the budget placed them, so a record the ranking put first is cut last.
see design/superpowers/specs/2026-11-14-task-context-files-only-design.md (part E)
"""

from functools import partial

from sidegraph import server
from sidegraph.schema import DecisionKind
from tests.test_retrieval_named_first import FILE, _evidence, _record, _world

HOW = "narrow"


def _line(tag: str, kind: str = "gotcha") -> str:
    return f"- [{kind}] {tag}: " + "x" * 280


def _body(mistakes: list[str], decisions: list[str]) -> str:
    return (
        "guard\n\n## ⚠ Known mistakes & gotchas\n"
        + "\n".join(mistakes)
        + "\n\n## Decisions\n"
        + "\n".join(decisions)
    )


def test_hard_cap_cuts_record_lines_in_reverse_placement_order():
    named, m1, m2, other = _line("named", "adr"), _line("m1"), _line("m2"), _line("other", "adr")
    body = _body([m1, m2], [named, other])
    # Room for two of the four record lines.
    cap = len(body) - 300
    out = server._hard_cap(body, [], HOW, cap=cap, placed=[named, m1, m2, other])
    assert len(out) <= cap
    assert named in out and m1 in out
    assert m2 not in out and other not in out
    assert "from: records" in out


def test_hard_cap_cuts_an_evidence_line_before_any_record_placed_earlier():
    m1, named = _line("m1"), _line("named", "adr")
    evidence = "  evidence: " + "y" * 280
    body = _body([m1, evidence], [named])
    cap = len(body) - 100
    out = server._hard_cap(body, [], HOW, cap=cap, placed=[m1, named, evidence])
    assert m1 in out and named in out
    assert evidence not in out


def test_hard_cap_without_a_placement_order_cuts_the_decisions_section_first():
    named, m1, m2, other = _line("named", "adr"), _line("m1"), _line("m2"), _line("other", "adr")
    body = _body([m1, m2], [named, other])
    out = server._hard_cap(body, [], HOW, cap=len(body) - 300)
    assert m1 in out and m2 in out
    assert named not in out and other not in out
    assert "from: decisions" in out


def test_a_trailer_longer_than_the_cap_leaves_a_note_that_counts_and_names_every_cut():
    named, m1, m2, other = _line("named", "adr"), _line("m1"), _line("m2"), _line("other", "adr")
    body = _body([m1, m2], [named, other])
    # Advice that alone exceeds the cap: every line of the body goes, then the trailer's front.
    trailer = [f"advice line {i}: " + "z" * 60 for i in range(40)]
    placed = [named, m1, m2, other]
    with_order = server._hard_cap(body, trailer, HOW, cap=600, placed=placed)
    without = server._hard_cap(body, trailer, HOW, cap=600)
    assert len(with_order) <= 600

    def note(out: str) -> str:
        return out[out.index("[output cut") :].split("]")[0]

    # A cut line is counted once and every cut section is named. The ordered call merges the
    # decisions and the mistakes into one group, `records`; the rest of the note is the same.
    merged = note(without).replace("decisions, records", "records")
    assert note(with_order) == merged
    assert "text under no known heading" in note(with_order)
    assert "the start of the advice" in note(with_order)


def _named_world(tmp_path):
    store, reader, e = _world(tmp_path)
    named = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR, age_days=9)
    for i in range(4):
        _record(store, [e["bar"]], f"bar-gotcha-{i}", DecisionKind.GOTCHA, age_days=i)
    return store, reader, named


def test_a_named_symbols_adr_survives_the_hard_cap(tmp_path, monkeypatch):
    store, reader, named = _named_world(tmp_path)
    args = (store, reader, [FILE], [{"name": "foo", "file_path": FILE}], 4000, 6000)
    whole = server._get_task_context_impl(*args)
    assert "foo-adr" in whole and "bar-gotcha-3" in whole
    monkeypatch.setattr(server, "_hard_cap", partial(server._hard_cap, cap=len(whole) - 20))
    cut = server._get_task_context_impl(*args)
    assert "[output cut at" in cut
    # The named symbol's ADR was placed first, so it is cut last: the oldest of the other
    # symbol's gotchas, placed last, goes instead.
    assert f"(id: {named.id})" in cut
    assert "bar-gotcha-3" not in cut and "bar-gotcha-0" in cut
    store.close()


def test_query_decisions_cuts_in_reverse_placement_order_too(tmp_path, monkeypatch):
    store, reader, named = _named_world(tmp_path)
    args = (store, reader, [FILE], [{"name": "foo", "file_path": FILE}], 6000)
    whole = server._query_decisions_impl(*args)
    monkeypatch.setattr(server, "_hard_cap", partial(server._hard_cap, cap=len(whole) - 20))
    cut = server._query_decisions_impl(*args)
    assert "[output cut at" in cut
    assert f"(id: {named.id})" in cut and "bar-gotcha-3" not in cut
    store.close()


def test_an_evidence_line_is_cut_before_the_named_adr_in_a_real_answer(tmp_path, monkeypatch):
    store, reader, e = _world(tmp_path)
    named = _record(store, [e["foo"]], "foo-adr", DecisionKind.ADR, age_days=9)
    mistake = _record(store, [e["bar"]], "bar-gotcha", DecisionKind.GOTCHA)
    _evidence(store, mistake, "evidence " * 30)
    args = (store, reader, [FILE], [{"name": "foo", "file_path": FILE}], 4000, 6000)
    whole = server._get_task_context_impl(*args)
    monkeypatch.setattr(server, "_hard_cap", partial(server._hard_cap, cap=len(whole) - 20))
    cut = server._get_task_context_impl(*args)
    assert f"(id: {named.id})" in cut and f"(id: {mistake.id})" in cut
    assert "  evidence: " not in cut
    store.close()
