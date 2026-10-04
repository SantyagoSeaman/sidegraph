"""``retrieve_decisions`` never hands over the whole store.

One call used to return a full dump of every visible decision: about 143k tokens on this
repository's own store. The listing now has two shapes, an overview (no narrowing filter) and a
filtered answer (full records, budget-bounded), and the serialized answer never exceeds the
clamped ``budget_chars``. The policy core (``_retrieve_decisions_impl``) is untouched; its own
tests live in ``test_proposal_lifecycle.py``, ``test_server_capture.py`` and
``test_ratify_policy.py``.
# see design/superpowers/specs/2026-10-04-bounded-decision-listing-design.md (D1-D6, tests T1-T15)
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fastmcp
import pytest

import sidegraph.server as server_module
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.server import _decision_listing
from sidegraph.store import Store

ROW_KEYS = {"id", "kind", "status", "title", "valid_from"}
REPO_ROOT = Path(__file__).resolve().parent.parent


def _add(
    store: Store,
    title: str,
    *,
    kind: str = "gotcha",
    status: str = "accepted",
    context: str = "c",
    choice: str = "ch",
    rejected: str | None = None,
    consequences: str | None = None,
    hours_ago: float = 1,
    supersedes: str | None = None,
) -> Decision:
    """One record, written through the real store. ``rejected`` status goes through the
    proposed -> dropped path, the only one that writes it."""
    wanted_rejected = status == "rejected"
    decision = Decision(
        title=title,
        kind=DecisionKind(kind),
        status=DecisionStatus.PROPOSED if wanted_rejected else DecisionStatus(status),
        context=context,
        choice=choice,
        rejected=rejected,
        consequences=consequences,
        valid_from=datetime.now(UTC) - timedelta(hours=hours_ago),
        supersedes=supersedes,
        provenance=Provenance(source="manual"),
    )
    store.add_decision(decision)
    if wanted_rejected:
        decision = store.drop(decision.id)[0]
    return decision


def _supersede(store: Store, old: Decision, title: str = "replacement") -> Decision:
    """Replace ``old`` the way the store does: the old one becomes superseded."""
    return _add(store, title, kind=old.kind.value, hours_ago=0, supersedes=old.id)


def _ask(store: Store, reader=None, **kwargs) -> dict:
    """The answer object, parsed from the text the tool would send."""
    return json.loads(_decision_listing(store, reader, **kwargs))


def _ids(answer: dict) -> list[str]:
    return [d["id"] for d in answer["decisions"]]


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# -- T1, T2: the overview ----------------------------------------------------------------


def test_t1_no_filter_is_the_overview(store):
    live = [_add(store, f"live {i}", kind="adr", hours_ago=10 + i) for i in range(6)]
    old = _add(store, "old way", kind="lesson", hours_ago=50)
    new = _supersede(store, old, "new way")
    dropped = _add(store, "dropped idea", kind="gotcha", status="rejected", hours_ago=60)

    answer = _ask(store, limit=4)

    assert answer["overview"] is True
    assert answer["total"] == 9  # 6 live + old + new way + dropped, superseded and rejected too
    assert answer["counts"]["status"] == {"accepted": 7, "superseded": 1, "rejected": 1}
    assert answer["counts"]["kind"] == {"adr": 6, "lesson": 2, "gotcha": 1}
    assert len(answer["newest"]) == 4
    assert all(set(row) == ROW_KEYS for row in answer["newest"])
    stamps = [row["valid_from"] for row in answer["newest"]]
    assert stamps == sorted(stamps, reverse=True)
    shown = {row["id"] for row in answer["newest"]}
    assert old.id not in shown and dropped.id not in shown  # the default hides history
    assert shown <= {d.id for d in live} | {new.id}
    assert answer["newest"][0]["id"] == new.id
    assert answer["hint"]


def test_t2_include_superseded_alone_is_still_the_overview(store):
    old = _add(store, "old way", hours_ago=3)
    new = _supersede(store, old, "new way")
    for i in range(5):
        _add(store, f"older {i}", hours_ago=20 + i)

    answer = _ask(store, include_superseded=True, limit=3)

    assert answer["overview"] is True
    assert "decisions" not in answer
    assert len(answer["newest"]) == 3
    assert [row["id"] for row in answer["newest"][:2]] == [new.id, old.id]
    assert answer["newest"][1]["status"] == "superseded"


# -- T3, T4: status and kind -------------------------------------------------------------


def test_t3_status_accepts_a_string_and_a_list_and_returns_full_records(store):
    old = _add(store, "old way", hours_ago=5)
    _supersede(store, old, "new way")
    dropped = _add(store, "dropped idea", status="rejected", hours_ago=4)
    _add(store, "plain", hours_ago=6)

    one = _ask(store, status="superseded")
    many = _ask(store, status=["superseded", "Rejected"])

    assert one["overview"] is False
    assert _ids(one) == [old.id]
    assert set(_ids(many)) == {old.id, dropped.id}
    assert {d["status"] for d in many["decisions"]} == {"superseded", "rejected"}
    assert many["decisions"][0]["context"] == "c"  # a full record, not a compact row
    assert many["matched"] == 2 and many["returned"] == 2 and many["omitted"] == 0


def test_t3_an_unknown_status_names_the_valid_values(store):
    _add(store, "plain")

    with pytest.raises(ValueError) as caught:
        _decision_listing(store, None, status="bogus")

    for valid in ("proposed", "accepted", "superseded", "rejected", "deprecated"):
        assert valid in str(caught.value)


def test_t4_kind_returns_that_kind_only(store):
    gotcha = _add(store, "a gotcha", kind="gotcha")
    _add(store, "an adr", kind="adr")
    _add(store, "a lesson", kind="lesson")

    answer = _ask(store, kind="gotcha")

    assert _ids(answer) == [gotcha.id]
    with pytest.raises(ValueError, match="constraint"):
        _decision_listing(store, None, kind="nonsense")


# -- T5: files ---------------------------------------------------------------------------


class _Repo:
    """A git repository with a graph and a store, each record anchored the way the test names.
    The graph holds ``pkg/a.py``, ``b.py`` and ``lib/c.py``; ``dup/x.py`` and ``other/x.py`` share
    a basename; ``big/`` is a directory of 30 files, too many for the ladder to read."""

    def __init__(self, tmp_path: Path):
        self.root = (tmp_path / "repo").resolve()
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        files = [
            "pkg/a.py",
            "b.py",
            "lib/c.py",
            "dup/x.py",
            "other/x.py",
            *(f"big/f{i:02}.py" for i in range(30)),
        ]
        nodes = [
            {
                "id": f"f{i}",
                "label": f"fn_{i}()",
                "norm_label": f"fn_{i}()",
                "file_type": "code",
                "source_file": f,
                "community": 1,
            }
            for i, f in enumerate(files)
        ]
        graph = self.root / "graphify-out" / "graph.json"
        graph.parent.mkdir()
        graph.write_text(json.dumps({"built_at_commit": "x", "nodes": nodes, "links": []}))
        self.reader = GraphifyReader(graph)
        self.store = Store(self.root / ".sidegraph")

    def anchored(self, title: str, entity: Entity, *, binding: str = "live") -> Decision:
        entity = self.store.upsert_entity(entity)
        decision = _add(self.store, title)
        self.store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status=binding)
        )
        return decision

    def entity(self, name: str, path: str | None, node: str | None = None) -> Entity:
        return Entity(
            canonical_name=name,
            descriptor=Descriptor(name=name, file_path=path),
            last_seen_node_id=node,
        )


@pytest.fixture
def repo(tmp_path: Path):
    r = _Repo(tmp_path)
    r.on_a = r.anchored("on a", r.entity("fn_0", "pkg/a.py"))
    r.on_b = r.anchored("on b", r.entity("fn_1", "b.py"))
    r.orphaned = r.anchored("orphaned on a", r.entity("gone", "pkg/a.py"), binding="orphaned")
    # An entity with no stored path: only the engine mapping (last_seen_node_id) ties it to a.py.
    r.nopath = r.anchored("no path", r.entity("helper", None, node="f0"))
    # A file the graph no longer holds, still carrying an anchor: only the descriptor path finds it.
    r.vanished = r.anchored("on a vanished file", r.entity("vanished", "gone.py"))
    yield r
    r.store.close()


A_RECORDS = ("on_a", "orphaned", "nopath")


@pytest.mark.parametrize(
    "spelling",
    ["pkg/a.py", "./pkg/a.py", "ABSOLUTE", "a.py", "pkg/", "PKG/a.py"],
)
def test_t5_files_read_the_way_get_task_context_reads_them(repo, spelling):
    path = str(repo.root / "pkg" / "a.py") if spelling == "ABSOLUTE" else spelling

    answer = _ask(repo.store, repo.reader, files=[path])

    assert set(_ids(answer)) == {getattr(repo, name).id for name in A_RECORDS}
    assert repo.on_b.id not in _ids(answer)
    assert answer["unresolved_files"] == []
    assert answer["unresolved_omitted"] == 0


def test_t5_a_file_the_graph_no_longer_holds_is_found_by_its_stored_path(repo):
    for spelling in ("gone.py", "./gone.py"):
        answer = _ask(repo.store, repo.reader, files=[spelling])

        assert _ids(answer) == [repo.vanished.id]
        assert answer["unresolved_files"] == []
    assert _ids(_ask(repo.store, None, files=["gone.py"])) == [repo.vanished.id]


def test_t5_a_file_nothing_can_read_is_reported(repo):
    answer = _ask(repo.store, repo.reader, files=["nope.py"])

    assert answer["matched"] == 0
    assert answer["decisions"] == []
    assert answer["unresolved_files"] == [
        {"path": "nope.py", "reason": "not-in-graph", "candidates": []}
    ]
    assert answer["hint"]


def test_t5_without_a_graph_the_exact_path_still_matches(repo):
    exact = _ask(repo.store, None, files=["pkg/a.py"])
    dotted = _ask(repo.store, None, files=["./pkg/a.py"])
    missing = _ask(repo.store, None, files=["nope.py"])

    assert set(_ids(exact)) == {repo.on_a.id, repo.orphaned.id}  # the path-less one needs the graph
    assert _ids(dotted) == _ids(exact)
    assert missing["unresolved_files"] == [
        {"path": "nope.py", "reason": "no-graph", "candidates": []}
    ]


def test_t5_a_path_that_reads_and_one_that_does_not_are_told_apart(repo):
    answer = _ask(repo.store, repo.reader, files=["pkg/a.py", "./nope.py"])

    assert repo.on_a.id in _ids(answer)
    assert [(u["path"], u["reason"]) for u in answer["unresolved_files"]] == [
        ("nope.py", "not-in-graph")  # the normalized spelling
    ]
    assert "unresolved_files" in answer["hint"]  # the hint points at the list ...
    assert "nope.py" not in answer["hint"]  # ... and names no file itself


def test_t5_an_ambiguous_basename_is_reported_with_its_candidates(repo):
    answer = _ask(repo.store, repo.reader, files=["x.py"])

    assert answer["matched"] == 0
    assert answer["unresolved_files"] == [
        {"path": "x.py", "reason": "ambiguous", "candidates": ["dup/x.py", "other/x.py"]}
    ]


def test_t5_a_directory_too_large_to_read_is_reported_with_candidates(repo):
    answer = _ask(repo.store, repo.reader, files=["big/"])

    (entry,) = answer["unresolved_files"]
    assert entry["path"] == "big/" and entry["reason"] == "directory-too-large"
    assert entry["candidates"] == ["big/f00.py", "big/f01.py", "big/f02.py"]


def test_t5_a_file_the_graph_holds_with_nothing_anchored_is_not_unresolved(repo):
    answer = _ask(repo.store, repo.reader, files=["lib/c.py"])

    assert answer["matched"] == 0
    assert answer["unresolved_files"] == []
    assert "include_superseded" in answer["hint"]  # history may still be anchored there


def test_t5_unreadable_files_alone_do_not_send_the_caller_to_history(repo):
    answer = _ask(repo.store, repo.reader, files=["nope.py", "x.py"])

    assert answer["matched"] == 0
    assert len(answer["unresolved_files"]) == 2
    assert "include_superseded" not in answer["hint"]


def test_t5_files_and_status_combine_with_and(repo):
    successor = _supersede(
        repo.store, repo.on_a
    )  # on_a is now superseded; the successor has no anchor

    answer = _ask(repo.store, repo.reader, files=["pkg/a.py"], status="superseded")

    assert _ids(answer) == [repo.on_a.id]
    assert successor.id not in _ids(answer)


def test_a6_the_entities_are_read_once_per_call_not_once_per_file(repo, monkeypatch):
    reads = []
    real = repo.store.iter_concrete_entities
    monkeypatch.setattr(
        repo.store, "iter_concrete_entities", lambda: reads.append(1) or real(), raising=True
    )

    _ask(repo.store, repo.reader, files=["pkg/a.py", "b.py", "lib/c.py", "dup/x.py", "big/"])

    assert len(reads) == 1


# -- T6, T7: query and ids ---------------------------------------------------------------


def test_t6_every_query_word_must_occur_in_the_text_fields(store):
    both = _add(store, "Cache layout", consequences="Entries expire on a TTL.")
    _add(store, "Cache warmup", consequences="Nothing else.")
    _add(store, "Unrelated", context="ttl alone")

    answer = _ask(store, query="cache Ttl")

    assert _ids(answer) == [both.id]


def test_t6_a_query_that_matches_nothing_suggests_fewer_words(store):
    _add(store, "Cache layout")

    answer = _ask(store, query="cache zebra")

    assert answer["matched"] == 0
    assert "one or two" in answer["hint"]
    assert "cache zebra" in answer["hint"]


def test_t7_ids_return_a_superseded_record_without_status(store):
    old = _add(store, "old way")
    _supersede(store, old)

    answer = _ask(store, ids=[old.id])

    assert _ids(answer) == [old.id]
    assert answer["decisions"][0]["status"] == "superseded"
    assert _ask(store, ids=[old.id], status="accepted")["matched"] == 0


def test_t7_a_proposed_id_outside_its_window_is_not_returned(store, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "1")
    stale = _add(store, "stale proposal", status="proposed", hours_ago=24 * 5)
    fresh = _add(store, "fresh proposal", status="proposed", hours_ago=1)

    answer = _ask(store, ids=[stale.id, fresh.id])

    assert _ids(answer) == [fresh.id]
    assert _ask(store, ids=[stale.id])["matched"] == 0


# -- T8: the budget ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def big_store(tmp_path_factory):
    """300 records of about 2,000 characters, in every kind and in all five statuses."""
    s = Store(tmp_path_factory.mktemp("big") / "big.db")
    statuses = ["accepted"] * 6 + ["proposed", "deprecated"]
    kinds = ["gotcha", "lesson", "adr", "constraint"]
    for i in range(300):
        _add(
            s,
            f"Record {i}",
            kind=kinds[i % 4],
            status=statuses[i % len(statuses)],
            context=("payload context words " * 90)[:1990],
            hours_ago=i + 1,
        )
    old = _add(s, "Record old", hours_ago=999, context="payload " * 5)
    _supersede(s, old, "Record new")
    _add(s, "Record dropped", status="rejected", hours_ago=998, context="payload " * 5)
    yield s
    s.close()


ALL_STATUSES = ["proposed", "accepted", "superseded", "rejected", "deprecated"]
SHAPES = {
    "overview": {},
    "status": {"status": ALL_STATUSES},
    "query": {"query": "payload"},
}


def _clamped(budget: int) -> int:
    return max(4000, min(60000, budget))


@pytest.mark.parametrize("limit", [25, 100])
@pytest.mark.parametrize("budget", [4000, 24000, 60000, 10**9])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_t8_the_serialized_answer_stays_within_the_clamped_budget(big_store, shape, budget, limit):
    text = _decision_listing(
        big_store, None, limit=limit, budget_chars=budget, include_superseded=True, **SHAPES[shape]
    )
    answer = json.loads(text)

    assert len(text) <= _clamped(budget)
    if shape == "overview":
        assert 0 < len(answer["newest"]) <= limit
    else:
        assert answer["matched"] >= 300
        assert 0 < answer["returned"] <= limit
        assert answer["omitted"] == answer["matched"] - answer["returned"]
        assert len(answer["decisions"]) == answer["returned"]


# -- T9: order ---------------------------------------------------------------------------


def test_t9_gotchas_then_lessons_then_the_rest_newest_first(store):
    spec = [
        ("adr new", "adr", 1),
        ("gotcha old", "gotcha", 30),
        ("lesson new", "lesson", 2),
        ("constraint mid", "constraint", 15),
        ("gotcha new", "gotcha", 3),
        ("lesson old", "lesson", 40),
        ("adr old", "adr", 50),
    ]
    for title, kind, hours in spec:
        _add(store, title, kind=kind, hours_ago=hours)

    listed = _ask(store, status="accepted")

    assert [d["title"] for d in listed["decisions"]] == [
        "gotcha new",
        "gotcha old",
        "lesson new",
        "lesson old",
        "adr new",
        "constraint mid",
        "adr old",
    ]


def test_t9_equal_stamps_break_the_tie_by_id_descending(store):
    stamp = datetime.now(UTC) - timedelta(hours=1)
    made = []
    for i in range(3):
        d = Decision(
            title=f"tie {i}",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=stamp,
            provenance=Provenance(source="manual"),
        )
        made.append(store.add_decision(d).id)

    assert _ids(_ask(store, status="accepted")) == sorted(made, reverse=True)


# -- T10: the proposal policy ------------------------------------------------------------


def test_t10_unratified_off_hides_a_proposed_record_everywhere(store, monkeypatch):
    kept = _add(store, "accepted one", hours_ago=2)
    proposed = _add(store, "proposed one", status="proposed", hours_ago=1)

    visible = _ask(store)
    assert visible["counts"]["status"] == {"accepted": 1, "proposed": 1}

    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    overview = _ask(store)
    by_status = _ask(store, status="proposed")
    by_query = _ask(store, query="one")
    by_id = _ask(store, ids=[proposed.id])

    assert overview["total"] == 1
    assert overview["counts"]["status"] == {"accepted": 1}
    assert [row["id"] for row in overview["newest"]] == [kept.id]
    assert by_status["matched"] == 0 and by_id["matched"] == 0
    assert _ids(by_query) == [kept.id]


# -- T11: clamps -------------------------------------------------------------------------


def test_t11_limit_is_clamped_to_one_through_a_hundred(big_store):
    low = _ask(big_store, query="payload", limit=0, budget_chars=60000)
    high = _ask(big_store, query="payload", limit=1000, budget_chars=60000)

    assert low["returned"] == 1
    assert 1 < high["returned"] <= 100


def test_t11_budget_is_clamped_to_four_thousand_through_sixty_thousand(big_store):
    tiny = _decision_listing(big_store, None, query="payload", budget_chars=1)
    floor = _decision_listing(big_store, None, query="payload", budget_chars=4000)
    huge = _decision_listing(big_store, None, query="payload", limit=100, budget_chars=10**9)

    assert tiny == floor
    assert 0 < json.loads(floor)["returned"] < json.loads(huge)["returned"]
    assert len(huge) <= 60000
    assert len(huge) > 24000  # the ceiling is a ceiling, not the default


# -- T14: the hint names the bound that cut ----------------------------------------------


def test_t14_a_count_cut_names_the_limit_and_not_the_budget(big_store):
    answer = _ask(big_store, query="payload", limit=3, budget_chars=60000)

    assert answer["returned"] == 3
    assert "limit" in answer["hint"]
    assert "raise limit" in answer["hint"] and "max 100" in answer["hint"]
    assert "budget_chars" not in answer["hint"]


def test_t14_a_size_cut_names_the_budget_and_not_the_limit(big_store):
    answer = _ask(big_store, query="payload", limit=100, budget_chars=4000)

    assert 0 < answer["returned"] < 100
    assert "character budget" in answer["hint"]
    assert "raise budget_chars" in answer["hint"] and "max 60000" in answer["hint"]
    assert "raise limit" not in answer["hint"]


def test_t14_a_bound_at_its_maximum_is_not_offered(store, big_store):
    for i in range(120):
        _add(store, f"tiny {i}", hours_ago=i + 1)

    by_count = _ask(store, status="accepted", limit=100, budget_chars=60000)  # 120 tiny: count cuts
    by_size = _ask(big_store, query="payload", limit=100, budget_chars=60000)  # the size cuts

    assert by_count["returned"] == 100 and by_size["returned"] < 100
    assert "limit" in by_count["hint"] and "raise" not in by_count["hint"]
    assert "character budget" in by_size["hint"] and "raise" not in by_size["hint"]
    assert "Narrow" in by_count["hint"] and "Narrow" in by_size["hint"]


def test_t14_a_record_too_big_for_the_budget_says_so(store):
    _add(store, "huge", context="x" * 5000)

    answer = _ask(store, status="accepted", budget_chars=4000)

    assert answer["matched"] == 1 and answer["returned"] == 0 and answer["decisions"] == []
    assert "does not fit" in answer["hint"]


def test_t14_the_hint_is_null_when_nothing_is_left_out(store):
    _add(store, "only one")

    answer = _ask(store, status="accepted")

    assert answer["matched"] == 1 and answer["omitted"] == 0
    assert answer["hint"] is None


# -- T15: empty values are absent --------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "   "},
        {"files": []},
        {"files": ["  "]},
        {"status": []},
        {"status": ""},
        {"kind": []},
        {"ids": []},
        {"query": "", "files": [], "status": [], "kind": [], "ids": []},
    ],
)
def test_t15_empty_values_count_as_absent(store, kwargs):
    _add(store, "one")

    assert _ask(store, **kwargs)["overview"] is True


# -- T13: the tool shell, through the real MCP server -----------------------------------


def _through_the_server(store, monkeypatch, tmp_path, calls: list[dict]) -> list:
    """Each call's text, parsed, as a host would receive it (no graph: the path is missing)."""
    monkeypatch.setattr(server_module, "_store", store)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-such-graph.json"))

    async def run():
        async with fastmcp.Client(server_module.mcp) as client:
            return [await client.call_tool("retrieve_decisions", args) for args in calls]

    return [json.loads(r.content[0].text) for r in asyncio.run(run())]


def test_t13_the_tool_passes_every_argument_through(store, monkeypatch, tmp_path):
    anchored = _add(store, "anchored cache", kind="adr", hours_ago=2)
    entity = store.upsert_entity(
        Entity(canonical_name="fn", descriptor=Descriptor(name="fn", file_path="pkg/a.py"))
    )
    store.add_binding(AnchorBinding(record_id=anchored.id, entity_id=entity.entity_id, tier=2))
    old = _add(store, "old cache", kind="lesson", hours_ago=9)
    _supersede(store, old, "other lesson")
    for i in range(3):
        _add(store, f"filler {i}", kind="gotcha", hours_ago=20 + i)

    by_files, by_kind, by_ids, by_limit, by_history, by_budget = _through_the_server(
        store,
        monkeypatch,
        tmp_path,
        [
            {"files": ["pkg/a.py"]},
            {"kind": "lesson"},
            {"ids": [old.id]},
            {"status": "accepted", "limit": 2},
            {"query": "cache", "include_superseded": True},
            {"status": "accepted", "budget_chars": 1},
        ],
    )

    assert _ids(by_files) == [anchored.id]
    assert {d["kind"] for d in by_kind["decisions"]} == {"lesson"}
    assert _ids(by_ids) == [old.id]
    assert by_limit["returned"] == 2 and by_limit["omitted"] == by_limit["matched"] - 2
    assert old.id in _ids(by_history)  # include_superseded reached the listing
    assert by_budget["returned"] == by_budget["matched"] > 0  # budget_chars=1 became 4,000


def test_t13_the_tool_hands_the_graph_to_the_listing(repo, monkeypatch):
    """A basename only resolves through the graph, so a shell that never passes a reader fails."""
    monkeypatch.setattr(server_module, "_store", repo.store)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(repo.root / "graphify-out" / "graph.json"))

    async def run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool("retrieve_decisions", {"files": ["a.py"]})

    answer = json.loads(asyncio.run(run()).content[0].text)

    assert repo.on_a.id in _ids(answer)
    assert repo.on_b.id not in _ids(answer)
    assert answer["unresolved_files"] == []


def test_t3_an_unknown_status_is_a_tool_error_naming_the_valid_values(store, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "_store", store)

    async def run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool(
                "retrieve_decisions", {"status": "bogus"}, raise_on_error=False
            )

    result = asyncio.run(run())

    assert result.is_error
    assert "accepted" in result.content[0].text and "bogus" in result.content[0].text


# -- A1, A4: the ceiling holds when the caller's own text is hostile ------------------------


@pytest.mark.parametrize("char", ['"', "\\", "\x01"], ids=["quote", "backslash", "control"])
@pytest.mark.parametrize("graph", [False, True], ids=["no-graph", "graph"])
def test_a1_echoes_are_clipped_on_their_escaped_length(repo, char, graph):
    """JSON doubles a quote and sextuples a control character: a clip on raw length let 12 long
    paths, a query and an id push the answer over a 4,000 budget."""
    text = _decision_listing(
        repo.store,
        repo.reader if graph else None,
        files=[char * 200 + str(i) for i in range(12)],
        query=char * 200,
        ids=[char * 200],
        budget_chars=4000,
    )
    answer = json.loads(text)

    assert len(text) <= 4000
    # Clipped entries leave room for all ten, so nothing was dropped to make the answer fit.
    assert len(answer["unresolved_files"]) == 10 and answer["unresolved_omitted"] == 2
    assert char * 100 not in answer["hint"]
    assert all(char * 100 not in entry["path"] for entry in answer["unresolved_files"])


def test_a4_unresolved_files_are_capped_at_ten_and_the_rest_counted(repo):
    answer = _ask(repo.store, repo.reader, files=[f"nope{i}.py" for i in range(15)])

    assert [u["path"] for u in answer["unresolved_files"]] == [f"nope{i}.py" for i in range(10)]
    assert answer["unresolved_omitted"] == 5
    assert "5 more" in answer["hint"]


# -- T12: our own skills never dump the store --------------------------------------------

NARROWING = ("status=", "kind=", "files=", "query=", "ids=")
SKILLS = sorted((REPO_ROOT / "plugin" / "sidegraph" / "skills").glob("*/SKILL.md"))


def _calls(text: str) -> list[tuple[int, str]]:
    """Every ``retrieve_decisions(`` call in ``text`` as ``(line, arguments)``, up to the
    parenthesis that closes it."""
    found = []
    for match in re.finditer(r"retrieve_decisions\(", text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
        found.append((text.count("\n", 0, match.start()) + 1, text[match.end() : end - 1]))
    return found


def test_t12_no_skill_calls_retrieve_decisions_without_a_narrowing_filter():
    assert SKILLS, "no SKILL.md found under plugin/sidegraph/skills"
    unbounded, seen = [], 0
    for skill in SKILLS:
        for line, args in _calls(skill.read_text(encoding="utf-8")):
            seen += 1
            if not any(f in args for f in NARROWING):
                unbounded.append(f"{skill.parent.name}/SKILL.md:{line}: retrieve_decisions({args})")

    assert unbounded == []
    assert seen >= 4, "the scan found fewer calls than the skills make: the matcher is broken"
