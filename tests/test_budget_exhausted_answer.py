"""An answer that found records and could place none says so, instead of "No context found."

A memory budget smaller than the first record leaves the answer empty. That used to read the
same as a call that found nothing, so the caller could not tell "nothing is known here" from
"something is known, ask for more room".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sidegraph.schema import (
    AnchorBinding,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
    Scope,
)
from sidegraph.server import _get_task_context_impl, _query_decisions_impl
from tests.test_server_nearest_anchored import ONE_NODE, _Repo, _repo

LONG = "Long record " + "x" * 600


def _with_long_record(tmp_path) -> _Repo:
    repo = _repo(tmp_path, ["core/a.py"])
    repo.record("core/a.py", LONG)
    return repo


def _ask(
    repo: _Repo, memory_budget: int, files=("core/a.py",), structure_budget: int = ONE_NODE
) -> str:
    return _get_task_context_impl(
        repo.store,
        repo.reader,
        files=list(files),
        entities=None,
        structure_budget=structure_budget,
        memory_budget=memory_budget,
    )


def test_a_budget_too_small_for_the_first_record_is_not_reported_as_no_context(tmp_path):
    repo = _with_long_record(tmp_path)

    out = _ask(repo, 100)

    assert out == (
        "1 record found for what you named did not fit memory_budget=100: "
        "call again with a larger memory_budget to get it."
    )


def test_the_count_is_the_records_that_did_not_fit(tmp_path):
    repo = _with_long_record(tmp_path)
    repo.record("core/a.py", LONG + " again")

    out = _ask(repo, 100)

    assert out.startswith("2 records found for what you named did not fit memory_budget=100: ")
    assert out.split("\n\n")[0].endswith("to get them.")


def test_a_file_with_no_record_still_says_no_context_found(tmp_path):
    repo = _repo(tmp_path, ["core/a.py"])

    assert _ask(repo, 100).startswith("No context found.")


def test_a_budget_that_holds_the_record_returns_it(tmp_path):
    repo = _with_long_record(tmp_path)

    out = _ask(repo, 2000)

    assert "did not fit" not in out and "Long record" in out


def test_a_call_with_no_seed_keeps_its_empty_answer(tmp_path):
    """A global record the budget refused was not "found for what you named": nothing was named."""
    repo = _repo(tmp_path, ["core/a.py"])
    repo.record("core/a.py", LONG, scope=Scope.GLOBAL)

    out = _ask(repo, 100, files=())

    assert out.startswith("No context found.")
    assert "did not fit" not in out


def test_the_budget_answer_keeps_the_advice_blocks(tmp_path):
    repo = _repo(tmp_path, [])
    repo.record("core/a.py", LONG)

    out = _ask(repo, 100)

    # core/a.py is in no graph: the answer still says why, after the budget sentence.
    assert out.startswith("1 record found for what you named did not fit memory_budget=100:")
    assert "## Not in the code graph" in out


def test_query_decisions_names_budget_chars(tmp_path):
    repo = _with_long_record(tmp_path)

    out = _query_decisions_impl(repo.store, repo.reader, ["core/a.py"], None, 100)

    assert out == (
        "1 record found for what you named did not fit budget_chars=100: "
        "call again with a larger budget_chars to get it."
    )


def test_query_decisions_with_no_record_keeps_no_context_found(tmp_path):
    repo = _repo(tmp_path, ["core/a.py"])

    assert _query_decisions_impl(repo.store, repo.reader, ["core/a.py"], None, 100).startswith(
        "No context found."
    )


def test_query_decisions_with_no_seed_keeps_its_empty_answer(tmp_path):
    repo = _repo(tmp_path, ["core/a.py"])
    repo.record("core/a.py", LONG, scope=Scope.GLOBAL)

    out = _query_decisions_impl(repo.store, repo.reader, None, None, 100)

    assert out.startswith("No context found.")


# -- review round 1: only the records of the named seeds count ----------------------------------


def _neighbour_with_long_record(tmp_path, *, sibling: bool = False) -> _Repo:
    """``core/a.py`` has no record and calls ``net/c.py``, whose one record is too long."""
    files = ["core/a.py", "net/c.py", *(["core/b.py"] if sibling else [])]
    repo = _repo(tmp_path, files, calls=[("core/a.py", "net/c.py")])
    repo.record("net/c.py", LONG)
    if sibling:
        repo.record("core/b.py", "Short sibling")
    repo.fill(8)
    return repo


def test_a_refused_neighbour_record_is_not_a_record_of_the_named_file(tmp_path):
    repo = _neighbour_with_long_record(tmp_path)

    out = _ask(repo, 100, structure_budget=4000)

    assert "did not fit" not in out
    assert out.startswith("No context found.")


def test_a_refused_neighbour_record_keeps_the_nearest_only_lead(tmp_path):
    repo = _neighbour_with_long_record(tmp_path, sibling=True)

    out = _ask(repo, 150, structure_budget=4000)

    assert "did not fit" not in out
    assert out.startswith("No record is anchored to the files you named;")


def test_query_decisions_does_not_count_a_refused_neighbour_record(tmp_path):
    repo = _neighbour_with_long_record(tmp_path)

    out = _query_decisions_impl(repo.store, repo.reader, ["core/a.py"], None, 100)

    assert out.startswith("No context found.")


def test_a_refused_global_record_is_not_a_record_of_the_named_file(tmp_path):
    repo = _repo(tmp_path, ["core/a.py", "elsewhere/b.py"])
    repo.record("elsewhere/b.py", LONG, scope=Scope.GLOBAL)

    assert _ask(repo, 100).startswith("No context found.")
    assert _query_decisions_impl(repo.store, repo.reader, ["core/a.py"], None, 100).startswith(
        "No context found."
    )


def test_only_the_seed_records_are_counted_when_a_neighbour_record_is_refused_too(tmp_path):
    repo = _repo(tmp_path, ["core/a.py", "net/c.py"], calls=[("core/a.py", "net/c.py")])
    repo.record("core/a.py", LONG)
    repo.record("net/c.py", LONG + " neighbour")

    out = _ask(repo, 100, structure_budget=4000)

    assert out.startswith("1 record found for what you named did not fit memory_budget=100: ")


# -- review round 1: the largest budget cannot be raised ----------------------------------------


def test_at_the_largest_budget_the_answer_does_not_advise_a_larger_one(tmp_path):
    repo = _repo(tmp_path, ["a.py"])
    repo.record("a.py", "x" * 50000)

    out = _get_task_context_impl(
        repo.store,
        repo.reader,
        files=["a.py"],
        entities=None,
        structure_budget=ONE_NODE,
        memory_budget=100000,
    )
    asked = _query_decisions_impl(repo.store, repo.reader, ["a.py"], None, 100000)

    assert out == (
        "1 record found for what you named did not fit memory_budget=40000, the largest "
        "allowed: name fewer or narrower seeds, or read it with retrieve_decisions(files=[...])."
    )
    assert asked == (
        "1 record found for what you named did not fit budget_chars=40000, the largest "
        "allowed: name fewer or narrower seeds, or read it with retrieve_decisions(files=[...])."
    )
    assert "larger" not in out + asked


def test_query_decisions_reports_the_clamped_budget_it_asked_for(tmp_path):
    """Below the maximum the sentence still names the budget the call really had."""
    repo = _repo(tmp_path, ["a.py"])
    repo.record("a.py", "x" * 50000)
    repo.record("a.py", "y" * 50000)

    out = _query_decisions_impl(repo.store, repo.reader, ["a.py"], None, 10**9)

    assert out.startswith("2 records found for what you named did not fit budget_chars=40000, ")


def test_a_refused_fact_of_the_named_file_counts_as_a_record_found_for_it(tmp_path):
    repo = _repo(tmp_path, ["core/a.py"])
    entity = repo.store.upsert_entity(
        Entity(canonical_name="fn_0", descriptor=Descriptor(name="fn_0", file_path="core/a.py"))
    )
    fact = repo.store.add_fact(
        Fact(
            statement="A long fact " + "x" * 600,
            source="bench",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC) - timedelta(days=1),
            provenance=Provenance(source="manual"),
        )
    )
    repo.store.add_binding(
        AnchorBinding(record_id=fact.id, entity_id=entity.entity_id, tier=2, status="live")
    )

    out = _ask(repo, 100)

    assert out.startswith("1 record found for what you named did not fit memory_budget=100: ")
