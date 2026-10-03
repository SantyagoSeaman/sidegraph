"""The directory walk of the nearest-anchored lookup, on the paths a caller should never send.

``get_task_context`` filters its seeds before they reach the walk, but the walk is the one loop
in the lookup that depends on its input, so it is held to ending for any string.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D11)
"""

from __future__ import annotations

import threading

import pytest

from tests.test_seed_ladder import make_reader

# `pkg` holds 3 of 15 anchored files, so it is not a hub.
ANCHORED = {
    "pkg/a.py": 2,
    "pkg/b.py": 1,
    "other/c.py": 1,
    **{f"z{i}/f.py": 1 for i in range(12)},
}


def _within(seconds: float, fn, *args, **kwargs):
    """``fn``'s result, or a failure when it does not return: a walk that never ends must fail
    the test, not hang the run."""
    box: list = []
    worker = threading.Thread(target=lambda: box.append(fn(*args, **kwargs)), daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, f"{fn.__name__} did not return within {seconds}s"
    return box[0]


@pytest.mark.parametrize(
    "path", ["/somewhere/else/x.py", "/x.py", ".", "", "../x.py", "a//b.py", "top.py"]
)
def test_the_walk_ends_for_any_path_and_names_nothing_outside_the_repository(tmp_path, path):
    from sidegraph import nearest_anchored

    reader = make_reader(tmp_path, ["pkg/a.py"])

    found = _within(5, nearest_anchored.nearest, path, ANCHORED, reader, in_graph=False)

    assert found == []


def test_the_walk_still_finds_the_directory_of_a_repository_path(tmp_path):
    from sidegraph import nearest_anchored

    reader = make_reader(tmp_path, ["pkg/a.py"])

    found = _within(5, nearest_anchored.nearest, "pkg/new.py", ANCHORED, reader, in_graph=False)

    assert [(n.path, n.how) for n in found] == [
        ("pkg/a.py", "same directory"),
        ("pkg/b.py", "same directory"),
    ]
