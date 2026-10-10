"""Redaction on the direct MCP write paths (add_decision / supersede_decision).

The propose/import pipelines have always redacted first; the direct paths used to write
verbatim — a documented gap (2026-07-10 skills-wave audit) at odds with the redact-first
rule for a repo-committed store. These tests pin the parity: same ``capture.redact``
patterns, same tag treatment (redact before slugify, never mint ``tag:redacted``), and a
``redactions`` count in the result mirroring ``ProposeResult``'s.
"""

from sidegraph.server import _add_decision_impl, _supersede_decision_impl
from sidegraph.store import Store

SECRET = "api_key=hunter2secret"


def test_add_decision_redacts_text_fields(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title=f"Rotate creds ({SECRET})",
        kind="gotcha",
        context=f"leaked {SECRET} in logs",
        choice=f"scrub {SECRET} everywhere",
        rejected=f"keeping {SECRET} inline",
        consequences=f"no more {SECRET}",
    )
    assert out["redactions"] == 5
    stored = store.get_decision(out["id"])
    for field in (
        stored.title,
        stored.context,
        stored.choice,
        stored.rejected,
        stored.consequences,
    ):
        assert "hunter2secret" not in field
        assert "[REDACTED]" in field


def test_add_decision_clean_text_reports_zero_redactions(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    assert out["redactions"] == 0
    assert store.get_decision(out["id"]).title == "t"


def test_add_decision_redacts_tag_text_before_slugify(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _add_decision_impl(
        store,
        None,
        title="t",
        kind="adr",
        context="c",
        choice="ch",
        tags=[SECRET, "hot-path"],
    )
    assert out["redactions"] == 1
    # The all-secret tag redacts to "[REDACTED]" -> slug "redacted" -> skipped, never
    # minted (same rule as the propose pipeline); the clean tag still binds.
    names = {e["canonical_name"] for e in out["entities"]}
    assert "tag:hot-path" in names
    assert "tag:redacted" not in names
    assert not any("hunter2secret" in n for n in names)


def test_supersede_decision_redacts_text_fields(tmp_path):
    store = Store(tmp_path / "srv.db")
    old = _add_decision_impl(store, None, title="old", kind="adr", context="c", choice="ch")
    out = _supersede_decision_impl(
        store,
        None,
        old_decision_id=old["id"],
        title=f"Reversed ({SECRET})",
        kind="adr",
        context=f"why: {SECRET}",
        choice=f"now: {SECRET}",
        rejected=f"was: {SECRET}",
        consequences=f"so: {SECRET}",
    )
    assert out["redactions"] == 5
    stored = store.get_decision(out["id"])
    for field in (
        stored.title,
        stored.context,
        stored.choice,
        stored.rejected,
        stored.consequences,
    ):
        assert "hunter2secret" not in field
        assert "[REDACTED]" in field


def test_supersede_decision_clean_text_reports_zero_redactions(tmp_path):
    store = Store(tmp_path / "srv.db")
    old = _add_decision_impl(store, None, title="old", kind="adr", context="c", choice="ch")
    out = _supersede_decision_impl(
        store,
        None,
        old_decision_id=old["id"],
        title="new",
        kind="adr",
        context="c2",
        choice="ch2",
    )
    assert out["redactions"] == 0


def test_add_decision_duplicate_secret_tags_counted_and_safe_binding_unique(tmp_path):
    with Store(tmp_path / "srv.db") as store:
        out = _add_decision_impl(
            store,
            None,
            title="t",
            kind="adr",
            context="c",
            choice="ch",
            tags=[SECRET, SECRET, "Safe", "safe"],
        )
        assert out["redactions"] == 2
        names = [
            store.get_entity(b.entity_id).canonical_name
            for b in store.bindings_for_record(out["id"])
            if b.tier == 0
        ]
        assert names == ["tag:safe"]


def test_add_decision_string_tags_redacted_before_comma_split(tmp_path):
    marker = "second" + "secretmarker"
    with Store(tmp_path / "srv.db") as store:
        result = _add_decision_impl(
            store,
            None,
            title="t",
            kind="adr",
            context="c",
            choice="ch",
            tags="password" + "=hunter," + marker,
        )
        assert result["redactions"] == 1
        assert store.find_abstract_entity("tag:" + marker) is None
        assert not any(b.tier == 0 for b in store.bindings_for_record(result["id"]))
