"""``get_task_context`` says where the memory is when a file seed has none of its own.

A file seed that no graph read could place, or that resolved with no open record anchored to
it, gets one sentence naming the nearest files that do carry records: graph neighbours first,
then the nearest directory. When the main answer has no decision memory at all, those files'
records follow under their own heading. The graphs are synthetic: one code node per file
(``fn_<i>()``), calls linking the files a test names, and records stored under the entity of
the file they are about.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D10-D12, tests B1-B5)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph import server
from sidegraph.retrieval import (
    _STANDING_SUPERSEDE_HINT,
    MEMORY_GUARD_LINE,
    RetrievalBudget,
    Seed,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.server import _get_task_context_impl
from sidegraph.store import Store
from tests.test_seed_ladder import make_reader

NEAREST = "## Nearest anchored"
RECORDS = "## Nearest anchored records (not anchored to your files)"
EMPTY = "## Why this is empty"
NOT_IN_GRAPH = "## Not in the code graph"

# A structure budget of 120 characters walks one node: the seed's own. A hub's neighbourhood
# overflows the node cap the same way, which is how a graph neighbour's records stay out of the
# main answer's Related block.
ONE_NODE = 120


class _Repo:
    """A synthetic graph and its store."""

    def __init__(self, tmp_path, files: list[str], calls: list[tuple[str, str]] | None = None):
        self.files = files
        links = [
            {"source": self._node(a), "target": self._node(b), "relation": "calls"}
            for a, b in calls or []
        ]
        self.reader = make_reader(tmp_path, files, links=links)
        self.store = Store(tmp_path / "t.db")
        self._entities: dict[str, Entity] = {}

    def _node(self, path: str) -> str:
        return f"f{self.files.index(path)}"

    def record(
        self,
        path: str,
        title: str,
        *,
        kind: DecisionKind = DecisionKind.GOTCHA,
        status: DecisionStatus = DecisionStatus.ACCEPTED,
        binding: str = "live",
        scope: Scope = Scope.REPO,
        valid_to: datetime | None = None,
    ) -> Decision:
        """A record about the symbol in ``path``."""
        if path not in self._entities:
            name = f"fn_{self.files.index(path)}" if path in self.files else "ghost"
            self._entities[path] = self.store.upsert_entity(
                Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=path))
            )
        now = datetime.now(UTC)
        decision = self.store.add_decision(
            Decision(
                title=title,
                kind=kind,
                status=status,
                scope=scope,
                context="c",
                choice="ch",
                valid_from=now - timedelta(days=2),
                valid_to=valid_to,
                provenance=Provenance(source="manual"),
            )
        )
        self.store.add_binding(
            AnchorBinding(
                record_id=decision.id,
                entity_id=self._entities[path].entity_id,
                tier=2,
                status=binding,
            )
        )
        return decision

    def fill(self, n: int) -> None:
        """``n`` unrelated anchored files in a directory each, one record apiece: the
        denominator the hub guard divides by."""
        for i in range(n):
            self.record(f"z{i}/f.py", f"Filler {i}")

    def call(self, files=None, entities=None, structure_budget: int = 4000) -> str:
        return _get_task_context_impl(
            self.store,
            self.reader,
            files=files,
            entities=entities,
            structure_budget=structure_budget,
            memory_budget=2000,
        )


def _repo(tmp_path, files: list[str], calls=None, fillers: int = 8) -> _Repo:
    """``files`` plus ``fillers`` extra files, each holding one record, so that no directory a
    test names holds more than a quarter of the anchored files by accident."""
    return _Repo(tmp_path, [*files, *(f"z{i}/f.py" for i in range(fillers))], calls)


def _area(tmp_path) -> _Repo:
    """``core/a.py`` has no record. Its same-directory sibling ``core/b.py`` has three, and
    ``net/c.py``, which ``core/a.py`` calls, has two."""
    repo = _repo(
        tmp_path,
        ["core/a.py", "core/b.py", "net/c.py"],
        calls=[("core/a.py", "net/c.py")],
    )
    for i in (1, 2, 3):
        repo.record("core/b.py", f"Sibling gotcha {i}")
    for i in (1, 2):
        repo.record("net/c.py", f"Neighbour gotcha {i}")
    repo.fill(8)
    return repo


def _section(out: str, heading: str) -> list[str]:
    """The lines of one block, heading excluded, up to the next blank line."""
    assert heading in out, out
    return out.split(heading + "\n", 1)[1].split("\n\n", 1)[0].splitlines()


# -- B1: the sentence, and the records when the main answer has none ------------------------


def test_b1_an_unanchored_file_names_its_neighbour_first_then_its_sibling(tmp_path):
    repo = _area(tmp_path)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `core/a.py`. Nearest anchored: "
        "`net/c.py` (linked in the code graph, 2 records), "
        "`core/b.py` (same directory, 3 records)."
    ]


def test_b1_with_no_memory_in_the_main_answer_the_records_follow_under_their_own_heading(
    tmp_path,
):
    repo = _area(tmp_path)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert out.index(NEAREST + "\n") < out.index(RECORDS + "\n")
    records = out.split(RECORDS + "\n", 1)[1]
    for title in ("Neighbour gotcha 1", "Neighbour gotcha 2", "Sibling gotcha 3"):
        assert title in records
    # One guard line for the reply (the map's own), and the headings sit one level lower.
    assert out.count(MEMORY_GUARD_LINE) == 1
    assert "### ⚠ Known mistakes & gotchas" in records
    assert re.search(r"(?m)^## ", records) is None
    # The supersede hint closes the reply once: the records carry ids, the main answer none.
    assert out.count(_STANDING_SUPERSEDE_HINT) == 1
    assert out.endswith("\n\n" + _STANDING_SUPERSEDE_HINT)


def test_b1_a_seed_with_records_of_its_own_gets_no_sentence(tmp_path):
    repo = _area(tmp_path)

    out = repo.call(files=["core/b.py"])

    assert NEAREST not in out and RECORDS not in out and EMPTY not in out


def test_b1_a_file_whose_only_records_are_not_open_is_unanchored(tmp_path):
    """A rejected record, an expired one and an orphaned binding are not memory the file has.
    The file reads as unanchored, and none of them makes a neighbour."""
    repo = _repo(tmp_path, ["core/a.py", "core/b.py", "core/c.py", "core/d.py"])
    repo.record("core/a.py", "Rejected", status=DecisionStatus.REJECTED)
    repo.record("core/a.py", "Expired", valid_to=datetime.now(UTC) - timedelta(days=1))
    repo.record("core/a.py", "Orphaned", binding="orphaned")
    repo.record("core/b.py", "Rejected too", status=DecisionStatus.REJECTED)
    repo.record("core/c.py", "Open one")
    repo.fill(8)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `core/a.py`. Nearest anchored: "
        "`core/c.py` (same directory, 1 record)."
    ]


def test_b1_a_degraded_binding_anchors_the_file_because_retrieval_shows_it(tmp_path):
    """Retrieval reaches a record through a live or a degraded binding, so a gotcha on
    ``core/a.py`` with a degraded one is under Mistakes, and the reply must not say that nothing
    is anchored there."""
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"])
    repo.record("core/a.py", "Degraded but shown", binding="degraded")
    repo.record("core/b.py", "A sibling")
    repo.fill(8)

    out = repo.call(files=["core/a.py"])

    assert "Degraded but shown" in out
    assert NEAREST not in out and "No current record" not in out


def test_b1_a_file_with_only_a_degraded_binding_is_a_neighbour(tmp_path):
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"])
    repo.record("core/b.py", "Degraded sibling", binding="degraded")
    repo.fill(8)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `core/a.py`. Nearest anchored: "
        "`core/b.py` (same directory, 1 record)."
    ]
    assert "Degraded sibling" in out


def test_b1_a_superseded_record_shown_as_tried_before_is_not_a_current_one(tmp_path):
    """The old record is under Related as a "tried, reverted" line, and it is not anchored: the
    sentence says *current*, so the two do not contradict each other."""
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"])
    old = repo.record("core/a.py", "The old way")
    repo.store.add_decision(
        Decision(
            title="The new way",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            supersedes=old.id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    repo.record("core/b.py", "A sibling")
    repo.fill(8)

    out = repo.call(files=["core/a.py"])

    assert "tried, reverted" in out and "The old way" in out
    assert _section(out, NEAREST) == [
        "No current record is anchored to `core/a.py`. Nearest anchored: "
        "`core/b.py` (same directory, 1 record)."
    ]


def test_b1_a_record_on_a_file_the_graph_does_not_hold_makes_no_neighbour(tmp_path):
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"])
    repo.record("core/gone.py", "About a deleted file")
    repo.fill(8)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert NEAREST not in out and "About a deleted file" not in out


def test_b1_a_deeper_neighbour_is_labelled_by_the_directory_it_sits_under(tmp_path):
    repo = _repo(tmp_path, ["svc/api/x.py", "svc/db/a.py"])
    repo.record("svc/db/a.py", "Under the service")
    repo.fill(8)

    out = repo.call(files=["svc/api/x.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `svc/api/x.py`. Nearest anchored: "
        "`svc/db/a.py` (under `svc`, 1 record)."
    ]


def test_b1_the_two_most_recorded_files_of_each_kind_are_named_ties_by_path(tmp_path):
    files = ["m/x.py", "n/a.py", "n/b.py", "n/c.py", "m/p.py", "m/q.py", "m/r.py"]
    repo = _repo(
        tmp_path,
        files,
        calls=[("m/x.py", "n/a.py"), ("m/x.py", "n/b.py"), ("m/x.py", "n/c.py")],
        fillers=14,
    )
    for path, count in {"n/a.py": 1, "n/b.py": 3, "n/c.py": 3, "m/p.py": 2, "m/q.py": 1}.items():
        for i in range(count):
            repo.record(path, f"{path} {i}")
    repo.fill(14)

    out = repo.call(files=["m/x.py"], structure_budget=ONE_NODE)

    # Neighbours: n/b.py and n/c.py (3 each, by path), not n/a.py. Directory: m/p.py, m/q.py,
    # but the per-seed cap of three files keeps the first three.
    assert _section(out, NEAREST) == [
        "No current record is anchored to `m/x.py`. Nearest anchored: "
        "`n/b.py` (linked in the code graph, 3 records), "
        "`n/c.py` (linked in the code graph, 3 records), "
        "`m/p.py` (same directory, 2 records)."
    ]


def test_b1_a_file_both_linked_and_in_the_same_directory_is_named_once(tmp_path):
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"], calls=[("core/a.py", "core/b.py")])
    repo.record("core/b.py", "One record")
    repo.fill(8)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `core/a.py`. Nearest anchored: "
        "`core/b.py` (linked in the code graph, 1 record)."
    ]


def test_b1_the_empty_answer_carries_the_sentence_in_the_why_block(tmp_path):
    """``core/new.py`` is in no graph: the answer is empty, so the sentence goes where the
    reason for an empty answer goes. The records have no guard line from the main answer to
    lean on, so the block brings the one the reply needs."""
    repo = _area(tmp_path)

    out = repo.call(files=["core/new.py"])

    assert out.startswith("No context found.\n\n")
    assert _section(out, EMPTY) == [
        "No current record is anchored to `core/new.py`. Nearest anchored: "
        "`core/b.py` (same directory, 3 records)."
    ]
    assert NEAREST + "\n" not in out
    assert out.index(NOT_IN_GRAPH) < out.index(EMPTY) < out.index(RECORDS)
    records = out.split(RECORDS + "\n", 1)[1]
    assert records.startswith(MEMORY_GUARD_LINE + "\n\n### ⚠ Known mistakes & gotchas")
    assert out.count(MEMORY_GUARD_LINE) == 1
    assert "Sibling gotcha 1" in records
    assert out.endswith("\n\n" + _STANDING_SUPERSEDE_HINT)


def test_b1_a_rewritten_seed_is_named_by_the_file_that_was_read(tmp_path):
    repo = _area(tmp_path)

    out = repo.call(files=["app/a.py"], structure_budget=ONE_NODE)

    assert "→ read as `core/a.py`" in out
    assert _section(out, NEAREST)[0].startswith("No current record is anchored to `core/a.py`.")


@pytest.mark.parametrize("seed", ["../x.py", "/somewhere/else/x.py", "."])
def test_b1_a_path_outside_the_repository_gets_no_sentence(tmp_path, seed):
    repo = _area(tmp_path)

    out = repo.call(files=[seed])

    assert NEAREST not in out and EMPTY + "\n" not in out and RECORDS not in out


def test_b1_an_entity_seed_gets_no_sentence(tmp_path):
    repo = _area(tmp_path)

    out = repo.call(entities=[{"name": "fn_0", "file_path": "core/a.py"}])

    assert NEAREST not in out and RECORDS not in out


def test_b1_nothing_anchored_in_the_store_adds_nothing(tmp_path):
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"], fillers=0)

    out = repo.call(files=["core/a.py", "core/new.py"])

    assert NEAREST not in out and RECORDS not in out and EMPTY not in out


# -- the lookup costs nothing for a seed that already has memory ---------------------------


def _count_builds(monkeypatch) -> list[int]:
    from sidegraph import nearest_anchored

    builds: list[int] = []
    real = nearest_anchored.anchored_files

    def counting(*args, **kwargs):
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(nearest_anchored, "anchored_files", counting)
    return builds


def test_a_seed_with_its_own_shown_records_never_builds_the_anchored_files(tmp_path, monkeypatch):
    repo = _area(tmp_path)
    builds = _count_builds(monkeypatch)

    out = repo.call(files=["core/b.py"])

    assert "Sibling gotcha 1" in out
    assert builds == []


def test_the_anchored_files_are_built_once_when_a_seed_has_none(tmp_path, monkeypatch):
    repo = _area(tmp_path)
    builds = _count_builds(monkeypatch)

    repo.call(files=["core/b.py", "core/a.py", "core/new.py"])

    assert builds == [1]


def test_a_shown_superseded_record_does_not_hide_the_unanchored_seed_from_the_lookup(
    tmp_path, monkeypatch
):
    """The shortcut counts only records that anchor a file: the "tried, reverted" line shown for
    ``core/a.py`` is not one, so the full lookup still runs and still names the sibling."""
    repo = _repo(tmp_path, ["core/a.py", "core/b.py"])
    old = repo.record("core/a.py", "The old way")
    repo.store.add_decision(
        Decision(
            title="The new way",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            supersedes=old.id,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    repo.record("core/b.py", "A sibling")
    repo.fill(8)
    builds = _count_builds(monkeypatch)

    out = repo.call(files=["core/a.py"])

    assert builds == [1]
    assert "`core/b.py` (same directory, 1 record)" in out


# -- B2: the hub guard and the root ---------------------------------------------------------


def _service(tmp_path, elsewhere: int) -> _Repo:
    """``svc/api/x.py`` has no record; five files under ``svc`` do, in other directories, and
    ``elsewhere`` files in directories of their own."""
    held = ["svc/db/a.py", "svc/db/b.py", "svc/ui/c.py", "svc/ui/d.py", "svc/core/e.py"]
    repo = _Repo(tmp_path, ["svc/api/x.py", *held, *(f"z{i}/f.py" for i in range(elsewhere))])
    for path in held:
        repo.record(path, f"About {path}")
    repo.fill(elsewhere)
    return repo


def test_b2_an_ancestor_holding_more_than_a_quarter_of_the_anchored_files_is_skipped(tmp_path):
    repo = _service(tmp_path, elsewhere=5)  # svc holds 5 of 10 anchored files: 50%

    out = repo.call(files=["svc/api/x.py"], structure_budget=ONE_NODE)

    assert NEAREST not in out and RECORDS not in out and "About svc" not in out


def test_b2_an_ancestor_holding_exactly_a_quarter_is_used(tmp_path):
    repo = _service(tmp_path, elsewhere=15)  # svc holds 5 of 20 anchored files: 25%

    out = repo.call(files=["svc/api/x.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `svc/api/x.py`. Nearest anchored: "
        "`svc/core/e.py` (under `svc`, 1 record), `svc/db/a.py` (under `svc`, 1 record)."
    ]


def test_b2_in_a_small_store_a_directory_with_a_few_files_is_not_a_hub(tmp_path):
    """Three of six anchored files is half the store, but three files are not a hub: the share
    rule only applies above a floor of four files (twice the two files a seed is given)."""
    repo = _Repo(
        tmp_path,
        ["app/x.py", "app/a.py", "app/b.py", "app/c.py", "lib/d.py", "lib/e.py", "lib/f.py"],
    )
    for path in ("app/a.py", "app/b.py", "app/c.py", "lib/d.py", "lib/e.py", "lib/f.py"):
        repo.record(path, f"About {path}")

    out = repo.call(files=["app/x.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST) == [
        "No current record is anchored to `app/x.py`. Nearest anchored: "
        "`app/a.py` (same directory, 1 record), `app/b.py` (same directory, 1 record)."
    ]


def test_b2_above_the_floor_a_directory_with_most_of_a_small_store_is_a_hub(tmp_path):
    """Five of eight anchored files is over a quarter and over the floor."""
    repo = _Repo(
        tmp_path,
        ["app/x.py", *(f"app/f{i}.py" for i in range(5)), *(f"lib/g{i}.py" for i in range(3))],
    )
    for i in range(5):
        repo.record(f"app/f{i}.py", f"About app {i}")
    for i in range(3):
        repo.record(f"lib/g{i}.py", f"About lib {i}")

    out = repo.call(files=["app/x.py"], structure_budget=ONE_NODE)

    assert NEAREST not in out and RECORDS not in out


def test_b2_the_repository_root_is_never_the_nearest_directory(tmp_path):
    """Nothing under ``lib`` is anchored, so the walk reaches the root. It holds every anchored
    file, four of them here, which is under the hub floor, so only the rule that the root is
    nobody's neighbourhood keeps it out. A file at the root starts there."""
    repo = _repo(tmp_path, ["lib/x.py", "top.py"], fillers=4)
    repo.fill(4)

    out = repo.call(files=["lib/x.py", "top.py"], structure_budget=ONE_NODE)

    assert NEAREST not in out and RECORDS not in out


# -- B3: a main answer with memory takes only the sentence ----------------------------------


def test_b3_when_the_main_answer_has_memory_only_the_sentence_is_added(tmp_path, monkeypatch):
    repo = _area(tmp_path)
    repo.record("z0/f.py", "A rule for everyone", kind=DecisionKind.CONSTRAINT, scope=Scope.GLOBAL)
    calls: list[int] = []
    real = server._retrieve

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_retrieve", counting)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert len(calls) == 1
    assert _section(out, NEAREST)[0].startswith("No current record is anchored to `core/a.py`.")
    assert RECORDS not in out
    assert out.count("A rule for everyone") == 1
    assert "Neighbour gotcha" not in out and "Sibling gotcha" not in out


def test_b3_a_neighbour_the_map_already_shows_is_not_shown_twice(tmp_path):
    """With room for the neighbourhood, ``net/c.py``'s records are already in the Related
    block of the main answer, so the second retrieval has nothing to add."""
    repo = _area(tmp_path)

    out = repo.call(files=["core/a.py"])

    assert out.count("Neighbour gotcha 1") == 1
    assert RECORDS not in out
    assert "`net/c.py` (linked in the code graph, 2 records)" in _section(out, NEAREST)[0]


# -- B4: telemetry --------------------------------------------------------------------------


def test_b4_shown_ids_merge_counters_sum_and_neighbours_are_not_seeds(tmp_path, monkeypatch):
    repo = _area(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    seen: dict[str, object] = {}
    real = server._retrieve

    def spying(*args, **kwargs):
        ctx = real(*args, **kwargs)
        seen.setdefault("contexts", []).append(ctx)  # type: ignore[union-attr]
        return ctx

    monkeypatch.setattr(server, "_retrieve", spying)

    repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    main, second = seen["contexts"]  # type: ignore[misc]
    assert main.shown_ids == [] and len(second.shown_ids) == 5
    # Every record the second retrieval showed counts as shown once.
    assert repo.store.retrieval_shows() == {rid: 1 for rid in second.shown_ids}
    (event,) = repo.store.render_events()
    assert event["emitted"] == second.emitted == 5
    assert event["selected"] == main.selected + second.selected
    assert event["chars_used"] == main.chars_used + second.chars_used > 0
    assert event["had_rejected"] == 0
    # The neighbour files are what was looked up, not what the agent asked about.
    assert repo.store.retrieval_seed_queries() == {"core/a.py": 1}


def test_b4_with_no_second_retrieval_the_counters_are_the_main_ones(tmp_path, monkeypatch):
    repo = _area(tmp_path)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")

    repo.call(files=["core/b.py"])

    (event,) = repo.store.render_events()
    assert event["emitted"] == 3
    assert repo.store.retrieval_seed_queries() == {"core/b.py": 1}


# -- B5: which seeds get a sentence ---------------------------------------------------------


def test_b5_a_directory_expansion_gets_no_sentence(tmp_path):
    repo = _repo(tmp_path, ["pkg/a.py", "pkg/b.py", "pkg/c.py"], fillers=12)
    repo.record("pkg/c.py", "About the directory")
    repo.fill(12)

    out = repo.call(files=["pkg/"], structure_budget=ONE_NODE)

    assert "→ a directory: read as all 3 files under it" in out
    assert NEAREST not in out and RECORDS not in out


def test_b5_an_ambiguous_seed_gets_no_sentence(tmp_path):
    repo = _repo(tmp_path, ["app/View.swift", "app/Other.swift", "lib/View.swift"])
    repo.record("app/Other.swift", "About a neighbour of a candidate")
    repo.fill(8)

    out = repo.call(files=["View.swift"], structure_budget=ONE_NODE)

    assert "→ ambiguous, did you mean `app/View.swift`, `lib/View.swift`?" in out
    assert NEAREST not in out and RECORDS not in out


def test_b5_at_most_three_seeds_get_a_sentence_and_the_rest_are_counted(tmp_path):
    files = [f"d{i}/{leaf}.py" for i in range(5) for leaf in ("x", "y")]
    repo = _repo(tmp_path, files, fillers=10)
    for i in range(5):
        repo.record(f"d{i}/y.py", f"Beside d{i}")
    repo.fill(10)

    out = repo.call(files=[f"d{i}/x.py" for i in range(5)], structure_budget=ONE_NODE)

    lines = _section(out, NEAREST)
    assert [line.split("`")[1] for line in lines[:3]] == ["d0/x.py", "d1/x.py", "d2/x.py"]
    assert lines[3:] == ["… and 2 more seeds with no anchored record"]
    # The records are those of the files the three sentences name.
    assert "Beside d2" in out and "Beside d3" not in out


def test_b5_a_spelling_variant_of_the_same_file_gets_one_sentence(tmp_path):
    repo = _area(tmp_path)

    out = repo.call(files=["core/a.py", "./core/a.py", "app/a.py"], structure_budget=ONE_NODE)

    sentences = [line for line in _section(out, NEAREST) if line.startswith("No current record")]
    assert len(sentences) == 1


# -- the blocks are advice: they never cost the answer --------------------------------------


def test_a_failure_in_the_plan_leaves_the_text_untouched(tmp_path, monkeypatch):
    from sidegraph import nearest_anchored

    repo = _area(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(nearest_anchored, "plan", boom)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    bare = server._retrieve(
        [Seed(file_path="core/a.py")], repo.store, repo.reader, RetrievalBudget(ONE_NODE, 2000)
    ).render()
    assert out == bare


def test_a_failing_second_retrieval_keeps_the_sentence(tmp_path, monkeypatch):
    repo = _area(tmp_path)
    real = server._retrieve
    calls: list[int] = []

    def second_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_retrieve", second_fails)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST)[0].startswith("No current record is anchored to `core/a.py`.")
    assert RECORDS not in out


def test_a_failure_in_the_records_block_leaves_the_rest_of_the_reply(tmp_path, monkeypatch):
    from sidegraph import nearest_anchored

    repo = _area(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(nearest_anchored, "records_block", boom)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST)[0].startswith("No current record is anchored to `core/a.py`.")
    assert RECORDS not in out and _STANDING_SUPERSEDE_HINT not in out


def test_a_failure_in_combining_the_two_contexts_keeps_the_sentence(tmp_path, monkeypatch):
    from sidegraph import nearest_anchored

    repo = _area(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(nearest_anchored, "combine", boom)

    out = repo.call(files=["core/a.py"], structure_budget=ONE_NODE)

    assert _section(out, NEAREST)[0].startswith("No current record is anchored to `core/a.py`.")
    assert RECORDS not in out
