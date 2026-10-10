"""Malformed drafts and failure causes must not expand secret exposure through diagnostics."""

import json

import pytest

from sidegraph import capture
from sidegraph.store import Store

MARKER = "opaque" + "S3Probe"


def decision(**updates):
    raw = dict(title="ordinary", kind="lesson", context="context", choice="choice")
    raw.update(updates)
    return raw


def fact(**updates):
    raw = dict(statement="observed", source="probe", anchors=[dict(name="sample.py")])
    raw.update(updates)
    return raw


def domain(**updates):
    raw = dict(slug="ordinary", title="ordinary", summary="summary")
    raw.update(updates)
    return raw


CASES = [
    (capture.propose, decision(kind=MARKER), decision(title="sibling"), "decisions"),
    (capture.propose, decision(title={MARKER: MARKER}), decision(title="sibling"), "decisions"),
    (capture.propose, decision(facts=[fact(source={MARKER: MARKER})]), decision(), "decisions"),
    (capture.propose_facts, fact(statement={MARKER: MARKER}), fact(), "facts"),
    (capture.propose_facts, fact(anchors=[dict(name={MARKER: MARKER})]), fact(), "facts"),
    (capture.propose_domains, domain(title={MARKER: MARKER}), domain(), "domains"),
    (capture.propose_domains, domain(slug=MARKER.upper()), domain(), "domains"),
]


def canonical(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*.json")}


def records(store, kind):
    return list(getattr(store, "iter_" + kind)())


@pytest.mark.parametrize("pipeline,bad,good,kind", CASES)
def test_invalid_draft_has_no_write_and_safe_reason(tmp_path, pipeline, bad, good, kind):
    path = tmp_path / "store"
    with Store(path) as store:
        before = canonical(path)
        result = pipeline([bad], store, None)[0]
        assert canonical(path) == before
    with Store(path) as reopened:
        assert records(reopened, kind) == []
    assert result.status == "rejected"
    assert MARKER.casefold() not in result.model_dump_json().casefold()
    assert "invalid" in result.reason
    with Store(path) as store:
        batch = pipeline([bad, good], store, None)
    with Store(path) as reopened:
        assert len(records(reopened, kind)) == 1
    assert batch[0].status == "rejected"
    assert batch[1].status in {"written", "proposed"}
    assert MARKER.casefold() not in json.dumps([r.model_dump() for r in batch]).casefold()


@pytest.mark.parametrize(
    "pipeline,raw,kind",
    [
        (capture.propose, decision(), "decisions"),
        (capture.propose_facts, fact(), "facts"),
        (capture.propose_domains, domain(), "domains"),
    ],
)
@pytest.mark.parametrize("write_first", [False, True])
def test_store_failure_reports_uncertain_write_without_cause(
    tmp_path, monkeypatch, pipeline, raw, kind, write_first
):
    path = tmp_path / "store"
    with Store(path) as store:
        before = canonical(path)
        method = "add_" + {"decisions": "decision", "facts": "fact", "domains": "domain"}[kind]
        original = getattr(store, method)

        def fail(record, *args, **kwargs):
            if write_first:
                original(record, *args, **kwargs)
            raise ValueError(MARKER)

        monkeypatch.setattr(store, method, fail)
        result = pipeline([raw], store, None)[0]
        if write_first:
            assert canonical(path) != before
        else:
            assert canonical(path) == before
    with Store(path) as reopened:
        assert len(records(reopened, kind)) == int(write_first)
    assert result.status == "rejected"
    assert MARKER.casefold() not in result.model_dump_json().casefold()
    assert "may be on disk" in result.reason
    assert "reopened" in result.reason
    assert "do not re-propose" in result.reason


def test_nested_fact_failure_remains_written_with_safe_recovery(tmp_path, monkeypatch):
    path = tmp_path / "store"

    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(capture, "_is_duplicate_fact", fail)
    with Store(path) as store:
        result = capture.propose([decision(facts=[fact()])], store, None)[0]
    with Store(path) as reopened:
        assert len(list(reopened.iter_decisions())) == 1
        assert list(reopened.iter_facts()) == []
    assert result.status == "written"
    assert result.facts[0].status == "rejected"
    assert MARKER.casefold() not in result.model_dump_json().casefold()
    assert "do not re-propose" in result.facts[0].reason


def test_post_write_anchor_failure_preserves_written_remedy(tmp_path, monkeypatch):
    path = tmp_path / "store"

    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(capture, "_bind_orphaned", fail)
    with Store(path) as store:
        result = capture.propose([decision(anchors=[dict(name="sample.py")])], store, None)[0]
    with Store(path) as reopened:
        assert len(list(reopened.iter_decisions())) == 1
    assert result.status == "written"
    assert MARKER.casefold() not in result.model_dump_json().casefold()
    assert "anchors" in result.reason and "drop" in result.reason


@pytest.mark.parametrize(
    "kind,method", [("adr", "ratify"), ("fact", "ratify_fact"), ("domain", "ratify_domains")]
)
def test_auto_ratification_exceptions_use_fixed_causes(tmp_path, monkeypatch, kind, method):
    def fail(*args, **kwargs):
        raise ValueError(MARKER)

    with Store(tmp_path / "store") as store:
        monkeypatch.setattr(store, method, fail)
        result = capture._auto_ratify(store, "missing", kind, capture.RatifyPolicy.AUTO_ALL)
    assert result.ratified_by is None
    assert MARKER not in result.error
    assert result.error


def test_returned_ratification_error_string_is_not_reflected(tmp_path, monkeypatch):
    with Store(tmp_path / "store") as store:
        monkeypatch.setattr(
            store, "ratify_domains", lambda **kwargs: {"missing": "error: " + MARKER}
        )
        result = capture._auto_ratify(store, "missing", "domain", capture.RatifyPolicy.AUTO_ALL)
    assert result.ratified_by is None
    assert MARKER not in result.error


def test_domain_parent_slug_rejection_does_not_echo_unknown_value(tmp_path):
    path = tmp_path / "store"
    with Store(path) as store:
        before = canonical(path)
        result = capture.propose_domains([domain(parent_slug=MARKER)], store, None)[0]
        assert canonical(path) == before
    with Store(path) as reopened:
        assert list(reopened.iter_domains()) == []
    assert MARKER not in result.model_dump_json()
    assert "parent_slug" in result.reason


def test_domain_path_warning_does_not_echo_offending_prefix(tmp_path):
    from pathlib import Path

    from sidegraph.engine.reader import GraphifyReader

    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")
    with Store(tmp_path / "store") as store:
        warnings = capture._lint_domain_path_prefixes([MARKER], reader, store)
    assert warnings
    assert MARKER not in json.dumps(warnings)
    assert "path_prefix" in warnings[0] and "dead prefix" in warnings[0]


@pytest.mark.parametrize("failure_stage", ["activation", "toc"])
def test_accepted_domain_retains_status_with_safe_failure_recovery(
    tmp_path, monkeypatch, failure_stage
):
    from pathlib import Path

    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.schema import DomainStatus

    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")

    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(
        capture, "activate_accepted_domain" if failure_stage == "activation" else "build_toc", fail
    )
    path = tmp_path / "store"
    with Store(path) as store:
        result = capture.propose_domains(
            [domain(seed_anchors=[dict(name="Trader", file_path="trader/exec.py")])],
            store,
            reader,
            ratify_policy=capture.RatifyPolicy.AUTO_ALL,
        )[0]
    with Store(path) as reopened:
        assert reopened.get_domain(result.domain_id).status == DomainStatus.ACCEPTED
    assert result.ratified_by == "auto:auto-all"
    assert MARKER not in result.model_dump_json()
    if failure_stage == "activation":
        assert result.auto_ratify_error.startswith("activation:")
    else:
        assert any("next sync" in warning for warning in result.warnings)
