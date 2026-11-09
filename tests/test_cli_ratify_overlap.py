"""``sidegraph-ratify`` answers an id named in both ``--accept`` and ``--drop`` the way the MCP
``ratify`` tool and ``Store.ratify_domains`` do: accepted, drop ignored. ``--all`` leaves out
what the same call drops, and the CLI closes the Store it opened.

# see design/superpowers/specs/2026-11-07-ratify-accept-drop-conflict-design.md
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import cli
from sidegraph.cli import ratify_main
from sidegraph.schema import DecisionStatus, DomainStatus, Fact, Provenance
from sidegraph.server import _add_domain_impl, _propose_decisions_impl, _ratify_impl
from sidegraph.store import Store

NOW = datetime.now(UTC)


def _decision(store: Store, title: str = "T1") -> str:
    out = _propose_decisions_impl(
        store,
        None,
        [dict(title=title, kind="lesson", context="c", choice="ch", anchors=[])],
    )
    return out[0]["decision_id"]


def _fact(store: Store, statement: str = "observed", supports: list[str] | None = None) -> str:
    fact = Fact(
        statement=statement,
        source="probe",
        supports=supports or [],
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    return store.add_fact(fact).id


def _domain(store: Store, slug: str = "payments") -> str:
    return _add_domain_impl(store, None, slug=slug, title=slug.title(), summary="s.")["domain_id"]


def _accepted_domain(store: Store) -> str:
    did = _domain(store)
    assert store.ratify_domains(accept=[did]) == {did: "accepted"}
    return did


def _status(path: Path, kind: str, record_id: str) -> str:
    with Store(path) as reopened:
        if kind == "decision":
            return reopened.get_decision(record_id).status.value
        if kind == "fact":
            return reopened.get_fact(record_id).status.value
        return reopened.get_domain(record_id).status.value


def _stamp(path: Path, kind: str, record_id: str) -> tuple[object, object]:
    """``(ratified_by, ratified_at)`` read from a reopened store. The ratifier identity can be
    ``None`` on a machine with no git identity, so tests compare stamps before and after, or
    to ``None`` for a record that must stay unratified, never to a name."""
    with Store(path) as reopened:
        record = {
            "decision": reopened.get_decision,
            "fact": reopened.get_fact,
            "domain": reopened.get_domain,
        }[kind](record_id)
        return record.ratified_by, record.ratified_at


def _seed(path: Path, kind: str) -> str:
    with Store(path) as store:
        if kind == "decision":
            return _decision(store)
        if kind == "fact":
            return _fact(store)
        if kind == "domain":
            return _domain(store)
        return _accepted_domain(store)


CASES = [
    ("domain", "accepted"),
    ("decision", "accepted"),
    ("fact", "accepted"),
    ("accepted domain", "accepted"),
]


@pytest.mark.parametrize("kind,final", CASES)
def test_same_input_same_state_through_cli_mcp_and_store(tmp_path, capsys, kind, final):
    record_kind = "domain" if kind == "accepted domain" else kind
    cli_path, mcp_path, store_path = (tmp_path / n for n in ("cli", "mcp", "store"))
    rid = _seed(cli_path, kind)
    stamp_before = _stamp(cli_path, record_kind, rid)
    capsys.readouterr()
    code = ratify_main(["--db", str(cli_path), "--accept", rid, "--drop", rid])
    captured = capsys.readouterr()
    accept_line = "already accepted" if kind == "accepted domain" else "accepted"
    assert code == 0
    assert captured.out.splitlines() == [f"{accept_line} {rid}", f"drop ignored {rid}"]
    assert "error" not in captured.err
    assert _status(cli_path, record_kind, rid) == final
    stamp_after = _stamp(cli_path, record_kind, rid)
    if kind == "accepted domain":
        assert stamp_after == stamp_before  # an accept that was already true rewrites nothing
    else:
        assert stamp_after[1] is not None  # the accept stamped when it ratified

    mid = _seed(mcp_path, kind)
    with Store(mcp_path) as store:
        out = _ratify_impl(store, accept=[mid], drop=[mid])
    assert out[mid].endswith("(drop ignored)")
    assert _status(mcp_path, record_kind, mid) == final

    if record_kind == "domain":
        sid = _seed(store_path, kind)
        with Store(store_path) as store:
            out = store.ratify_domains(accept=[sid], drop=[sid])
        assert out[sid].endswith("(drop ignored)")
        assert _status(store_path, "domain", sid) == final


def test_accepted_domain_dropped_alone_is_still_dropped(tmp_path, capsys):
    path = tmp_path / "s"
    rid = _seed(path, "accepted domain")
    capsys.readouterr()
    assert ratify_main(["--db", str(path), "--drop", rid]) == 0
    assert capsys.readouterr().out.splitlines() == [f"dropped {rid}"]
    assert _status(path, "domain", rid) == DomainStatus.DROPPED.value


def _slug_collision(path: Path) -> tuple[str, str]:
    """A proposed loser and an accepted rival that came to share one slug (a merged
    collision). Public insertion refuses duplicate live slugs, so the rival's slug is
    rewritten directly in this scratch store only."""
    with Store(path) as store:
        loser = _domain(store, "probe")
        rival_id = _domain(store, "rival")
        assert store.ratify_domains(accept=[rival_id]) == {rival_id: "accepted"}
        rival = store.get_domain(rival_id)
        rival.slug = "probe"
        with store._mutation():
            store._write_domain(rival)
    return loser, rival_id


def test_failed_accept_is_not_turned_into_a_drop(tmp_path, capsys):
    cli_path, mcp_path, probe_path = (tmp_path / n for n in ("cli", "mcp", "probe"))
    probe_loser, _ = _slug_collision(probe_path)
    with Store(probe_path) as store:
        refused = store.ratify_domains(accept=[probe_loser])
    assert refused[probe_loser].startswith("error")  # the fixture really refuses

    loser, _ = _slug_collision(cli_path)
    capsys.readouterr()
    code = ratify_main(["--db", str(cli_path), "--accept", loser, "--drop", loser])
    captured = capsys.readouterr()
    assert code == 1
    assert f"error {loser}" in captured.err
    assert f"drop ignored {loser}" in captured.out
    assert _status(cli_path, "domain", loser) == DomainStatus.PROPOSED.value
    assert _stamp(cli_path, "domain", loser) == (None, None)

    mcp_loser, _ = _slug_collision(mcp_path)
    with Store(mcp_path) as store:
        out = _ratify_impl(store, accept=[mcp_loser], drop=[mcp_loser])
    assert out[mcp_loser].startswith("error") and out[mcp_loser].endswith("(drop ignored)")
    assert _status(mcp_path, "domain", mcp_loser) == DomainStatus.PROPOSED.value


def _nested(path: Path) -> tuple[str, str]:
    with Store(path) as store:
        did = _decision(store)
        return did, _fact(store, supports=[did])


def test_dropping_a_fact_the_accept_cascade_covers_is_ignored(tmp_path, capsys):
    cli_path, mcp_path = tmp_path / "cli", tmp_path / "mcp"
    did, fid = _nested(cli_path)
    capsys.readouterr()
    assert ratify_main(["--db", str(cli_path), "--accept", did, "--drop", fid]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == [
        f"accepted {did}",
        f"accepted (evidence of {did}) {fid}",
        f"drop ignored {fid}",
    ]
    assert out.err == ""
    assert _status(cli_path, "decision", did) == "accepted"
    assert _status(cli_path, "fact", fid) == "accepted"

    mdid, mfid = _nested(mcp_path)
    with Store(mcp_path) as store:
        _ratify_impl(store, accept=[mdid], drop=[mfid])
    assert _status(mcp_path, "fact", mfid) == "accepted"


def test_partial_success_across_different_ids_is_unchanged(tmp_path, capsys):
    path = tmp_path / "s"
    with Store(path) as store:
        keep = _decision(store, "keep")
        drop = _decision(store, "drop")
    capsys.readouterr()
    assert ratify_main(["--db", str(path), "--accept", keep, "--drop", drop, "nope"]) == 1
    captured = capsys.readouterr()
    assert f"accepted {keep}" in captured.out
    assert "error nope" in captured.err
    assert _status(path, "decision", keep) == "accepted"
    assert _status(path, "decision", drop) == "rejected"


def test_all_leaves_out_a_domain_the_same_call_drops(tmp_path, capsys):
    path = tmp_path / "s"
    with Store(path) as store:
        domain = _domain(store)
        keep = _decision(store, "keep")
    capsys.readouterr()
    assert ratify_main(["--db", str(path), "--all", "--drop", domain]) == 0
    out = capsys.readouterr().out
    assert f"accepted {domain}" not in out
    assert f"dropped {domain}" in out
    assert _status(path, "domain", domain) == "dropped"
    assert _status(path, "decision", keep) == "accepted"


def test_all_leaves_out_a_decision_the_same_call_drops(tmp_path, capsys):
    path = tmp_path / "s"
    with Store(path) as store:
        keep = _decision(store, "keep")
        gone = _decision(store, "gone")
    capsys.readouterr()
    assert ratify_main(["--db", str(path), "--all", "--drop", gone]) == 0
    assert _status(path, "decision", keep) == "accepted"
    assert _status(path, "decision", gone) == "rejected"


def test_all_with_a_nested_fact_drop_follows_the_cascade(tmp_path, capsys):
    path = tmp_path / "s"
    did, fid = _nested(path)
    capsys.readouterr()
    assert ratify_main(["--db", str(path), "--all", "--drop", fid]) == 0
    out = capsys.readouterr()
    assert f"drop ignored {fid}" in out.out
    assert _status(path, "decision", did) == "accepted"
    assert _status(path, "fact", fid) == "accepted"


@pytest.mark.slow
def test_exit_code_through_the_real_entry_point(tmp_path):
    entry = Path(sys.executable).parent / "sidegraph-ratify"
    if not entry.exists():
        pytest.skip("sidegraph-ratify is not installed next to this interpreter")
    path = tmp_path / "s"
    with Store(path) as store:
        did = _decision(store)
    run = subprocess.run(
        [str(entry), "--db", str(path), "--accept", did, "--drop", did],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert _status(path, "decision", did) == "accepted"


class _Spy(Store):
    closes = 0

    def close(self) -> None:
        type(self).closes += 1
        super().close()


@pytest.fixture
def spy(monkeypatch):
    _Spy.closes = 0
    monkeypatch.setattr(cli, "Store", _Spy)
    return _Spy


def test_listing_and_batch_close_the_store_they_opened(tmp_path, spy, capsys):
    path = tmp_path / "s"
    with Store(path) as store:
        did = _decision(store)
    assert ratify_main(["--db", str(path)]) == 0
    assert spy.closes == 1
    assert ratify_main(["--db", str(path), "--accept", did]) == 0
    assert spy.closes == 2


def test_a_raising_batch_still_closes_the_store(tmp_path, spy, monkeypatch):
    path = tmp_path / "s"
    with Store(path) as store:
        did = _decision(store)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_route_ratify", boom)
    with pytest.raises(RuntimeError):
        ratify_main(["--db", str(path), "--accept", did])
    assert spy.closes == 1
    with Store(path) as fresh:  # the path is free for the next writer
        assert fresh.get_decision(did).status == DecisionStatus.PROPOSED
        assert _decision(fresh, "next") != did
    with Store(path) as reopened:
        assert len(list(reopened.iter_proposed())) == 2


def test_repeated_ids_in_one_list_are_routed_once(tmp_path, capsys):
    path = tmp_path / "s"
    with Store(path) as store:
        keep = _decision(store, "keep")
        gone = _decision(store, "gone")
    capsys.readouterr()
    assert ratify_main(["--db", str(path), "--accept", keep, keep, "--drop", gone, gone]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert [ln for ln in captured.out.splitlines() if keep in ln] == [f"accepted {keep}"]
    assert [ln for ln in captured.out.splitlines() if gone in ln] == [f"dropped {gone}"]
