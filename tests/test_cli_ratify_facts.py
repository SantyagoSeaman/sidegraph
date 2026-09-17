"""CLI ratify surface parity: fact routing, cascade reporting, nested queue render (Task 7).
see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

from datetime import UTC, datetime

from sidegraph.cli import ratify_main
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.store import Store

NOW = datetime.now(UTC)


def _decision(**kw):
    base = dict(
        title="use httpx",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Decision(**base)


def _fact(**kw):
    base = dict(
        statement="httpx has no built-in retry",
        source="httpx docs",
        valid_from=NOW,
        provenance=Provenance(source="test"),
    )
    base.update(kw)
    return Fact(**base)


def test_cli_bare_run_lists_facts_section(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    store.add_fact(_fact(statement="standalone one"))
    assert ratify_main(["--db", str(store_path)]) == 0
    out = capsys.readouterr().out
    assert "Facts:" in out and "standalone one" in out


def test_cli_accept_fact_id_directly(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    f = store.add_fact(_fact())
    assert ratify_main(["--db", str(store_path), "--accept", f.id]) == 0
    out = capsys.readouterr().out
    assert f"accepted {f.id}" in out
    with Store(store_path) as reopened:
        assert reopened.get_fact(f.id).status == DecisionStatus.ACCEPTED


def test_cli_all_includes_standalone_fact(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    f = store.add_fact(_fact())
    assert ratify_main(["--db", str(store_path), "--all"]) == 0
    with Store(store_path) as reopened:
        assert reopened.get_fact(f.id).status == DecisionStatus.ACCEPTED


def test_cli_cascade_reported(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    assert ratify_main(["--db", str(store_path), "--accept", d.id]) == 0
    out = capsys.readouterr().out
    assert f"accepted (evidence of {d.id}) {f.id}" in out


def test_cli_all_reports_nested_fact_once_via_cascade(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    assert ratify_main(["--db", str(store_path), "--all"]) == 0
    out = capsys.readouterr().out
    cascade_line = f"accepted (evidence of {d.id}) {f.id}"
    assert out.count(cascade_line) == 1
    assert out.count(f.id) == 1  # reported exactly once, never double
    assert "error" not in out.lower()
    with Store(store_path) as reopened:
        assert reopened.get_fact(f.id).status == DecisionStatus.ACCEPTED


# -- fix pass: accepting a decision AND its nested fact together must not yield a spurious
# error (mirrors test_server_ratify_facts.py's order-independence coverage).


def test_cli_accept_decision_and_nested_fact_together_exits_clean(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    assert ratify_main(["--db", str(store_path), "--accept", d.id, f.id]) == 0
    out = capsys.readouterr().out
    assert f"accepted (evidence of {d.id}) {f.id}" in out
    assert "error" not in out.lower()
    with Store(store_path) as reopened:
        assert reopened.get_fact(f.id).status == DecisionStatus.ACCEPTED
        assert reopened.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_cli_drop_decision_and_nested_fact_together_exits_clean(tmp_path, capsys):
    store_path = tmp_path / "s"
    store = Store(store_path)
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    assert ratify_main(["--db", str(store_path), "--drop", d.id, f.id]) == 0
    out = capsys.readouterr().out
    assert f"dropped (evidence of {d.id}) {f.id}" in out
    assert "error" not in out.lower()
    with Store(store_path) as reopened:
        assert reopened.get_fact(f.id).status == DecisionStatus.REJECTED
        assert reopened.get_decision(d.id).status == DecisionStatus.REJECTED
