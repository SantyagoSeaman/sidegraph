"""A fact statement in the task context is shown whole up to 700 characters; the SessionStart
map keeps the 240-character clip.
see design/superpowers/specs/2026-11-14-task-context-files-only-design.md (part C)
"""

from sidegraph.retrieval import _fmt_fact, get_task_context, top_tier_sections
from sidegraph.schema import AnchorBinding, DecisionKind, DecisionStatus, Fact, Provenance
from tests.test_retrieval_named_first import BASE, FILES_ONLY, _record, _world

WHOLE = ("word " * 140).strip()  # 699 characters
LONG = ("word " * 141).strip()  # 704 characters


def _fact(store, statement, *, entity=None, supports=None):
    fact = store.add_fact(
        Fact(
            statement=statement,
            source="s",
            status=DecisionStatus.ACCEPTED,
            supports=supports or [],
            valid_from=BASE,
            provenance=Provenance(source="manual"),
        )
    )
    if entity is not None:
        store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
        )
    return fact


def test_a_statement_up_to_700_characters_is_shown_whole(tmp_path):
    store, reader, e = _world(tmp_path)
    _fact(store, WHOLE, entity=e["foo"])
    (line,) = get_task_context(FILES_ONLY, store, reader).facts
    assert f"- fact: {WHOLE} [s]" in line
    assert "clipped" not in line


def test_a_longer_statement_is_clipped_at_700_with_the_note(tmp_path):
    store, reader, e = _world(tmp_path)
    fact = _fact(store, LONG, entity=e["foo"])
    (line,) = get_task_context(FILES_ONLY, store, reader).facts
    assert line.startswith(f"- fact: {WHOLE}… [s]")
    assert f'(clipped; full: list_facts(ids=["{fact.id}"]))' in line


def test_the_evidence_line_uses_the_same_clip(tmp_path):
    store, reader, e = _world(tmp_path)
    mistake = _record(store, [e["foo"]], "foo-gotcha", DecisionKind.GOTCHA)
    _fact(store, WHOLE, supports=[mistake.id])
    ctx = get_task_context(FILES_ONLY, store, reader)
    assert ctx.mistakes[1].startswith(f"  evidence: {WHOLE} [s]")
    assert "clipped" not in ctx.mistakes[1]


def test_the_session_start_map_keeps_the_240_clip(tmp_path):
    store, _reader, e = _world(tmp_path)
    fact = _fact(store, WHOLE, entity=e["foo"])
    sections = {s.heading: s for s in top_tier_sections(store, None)}
    (entry,) = sections["## Known facts"].entries
    shown = ("word " * 48).strip()  # 239 characters: the last whole word inside 240
    assert entry.text == f'- fact: {shown}… [s] (clipped; full: list_facts(ids=["{fact.id}"]))'
    assert _fmt_fact(fact) == entry.text
