"""MCP advice writes only the local advice journal (Portal design D9)."""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from sidegraph import server
from sidegraph.input_limits import MAX_FIELD_BYTES
from sidegraph.schema import DecisionStatus, DomainStatus
from sidegraph.store import Store
from sidegraph.write_boundary import WRITE_FAILURE
from tests.store_format2_support import git
from tests.store_record_support import _decision, _domain, _fact
from tests.test_bounded_listings import call
from tests.test_store_migration import _legacy_db, _write_sentinel


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    with Store(tmp_path / "store") as store:
        monkeypatch.setattr(server, "_store", store)
        yield store


def test_advise_non_proposed_refused(store: Store) -> None:
    accepted = store.add_decision(_decision())
    pending = store.add_decision(_decision("Draft", status=DecisionStatus.PROPOSED))
    before = store.canonical_digest()

    result = call(
        "advise_ratification",
        {
            "items": [
                {"id": accepted.id, "verdict": "accept", "reason": "Already settled."},
                {"id": "missing", "verdict": "reject", "reason": "Missing."},
                {"id": pending.id, "verdict": "edit", "reason": "Clarify the reason."},
            ]
        },
    )

    assert not result.is_error, result.content
    assert json.loads(result.content[0].text) == {
        accepted.id: "error: record is not proposed",
        "missing": "error: record is not proposed",
        pending.id: "recorded",
    }
    assert [row["record_id"] for row in store.advice_rows()] == [pending.id]
    assert store.canonical_digest() == before
    after = store.get_decision(pending.id)
    assert after is not None and after.status == DecisionStatus.PROPOSED


@pytest.mark.parametrize("kind", ["decision", "fact", "domain"])
def test_advise_replaces_row(store: Store, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if kind == "decision":
        record_id = store.add_decision(_decision(status=DecisionStatus.PROPOSED)).id
    elif kind == "fact":
        record_id = store.add_fact(_fact(status=DecisionStatus.PROPOSED)).id
    else:
        record_id = store.add_domain(_domain(status=DomainStatus.PROPOSED)).domain_id
    monkeypatch.setattr(server, "_session_key", lambda _: "advisor")
    first = call(
        "advise_ratification",
        {"items": [{"id": record_id, "verdict": "accept", "reason": "Looks good."}]},
    )
    assert not first.is_error, first.content

    second = call(
        "advise_ratification",
        {
            "items": [
                {
                    "id": record_id,
                    "verdict": "reject",
                    "reason": "Нужен источник. password=hunter2secret",
                }
            ]
        },
    )
    assert not second.is_error, second.content
    (row,) = store.advice_rows()
    assert row["record_id"] == record_id
    assert row["verdict"] == "reject"
    assert row["reason"] == "Нужен источник. [REDACTED]"
    assert row["session_id"] == "advisor"
    assert row["record_hash"] == store.record_body_hash(record_id)


def test_advise_reason_bounded(store: Store) -> None:
    pending = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    # This is over the UTF-8 byte cap, though under the cap in code points.
    result = call(
        "advise_ratification",
        {
            "items": [
                {"id": pending.id, "verdict": "accept", "reason": "é" * (MAX_FIELD_BYTES // 2 + 1)}
            ]
        },
    )

    assert result.is_error
    assert result.content[0].text == "Input exceeds resource limits."
    assert store.advice_rows() == []


def test_advise_reason_diagnostic_names_the_field(store: Store) -> None:
    result = call("advise_ratification", {"items": [{"id": "draft", "verdict": "accept"}]})

    assert result.is_error
    assert result.content[0].text == "invalid draft: reason: string_type"
    assert store.advice_rows() == []


def test_advise_missing_store_leaves_repository_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    monkeypatch.setattr(server, "_store", None)

    result = call(
        "advise_ratification",
        {"items": [{"id": "missing", "verdict": "accept", "reason": "Reviewed."}]},
    )

    assert result.is_error
    assert "no store" in result.content[0].text
    assert not (tmp_path / ".sidegraph").exists()


def test_cold_advice_uses_its_own_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".sidegraph"
    with Store(root) as store:
        draft = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    ignore = root / ".gitignore"
    ignore.unlink()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server, "_store", None)
    closed: list[Path] = []
    close = Store.close

    def record_close(store: Store) -> None:
        close(store)
        closed.append(store.path)

    monkeypatch.setattr(Store, "close", record_close)

    result = call(
        "advise_ratification",
        {"items": [{"id": draft.id, "verdict": "accept", "reason": "Reviewed."}]},
    )

    assert not result.is_error
    assert server._store is None
    assert closed == [root]
    assert not ignore.exists()
    with server._get_store() as ordinary:
        assert [row["record_id"] for row in ordinary.advice_rows()] == [draft.id]
    assert ignore.is_file()


def test_cold_advice_reports_a_missing_format_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".sidegraph"
    with Store(root) as store:
        store.add_decision(_decision())
    marker = root / "format"
    marker.unlink()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server, "_store", None)

    result = call("advise_ratification", {"items": []})

    assert result.is_error
    assert "format marker missing" in result.content[0].text
    assert not marker.exists()


def test_advise_store_moved_during_cold_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    path = tmp_path / ".sidegraph"
    with Store(path) as store:
        draft = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))
    monkeypatch.setattr(server, "_store", None)
    moved = tmp_path / "moved"
    check_requirement = Store._check_requirement

    def move_after_check(store: Store, *, force: bool = False) -> str | None:
        requirement = check_requirement(store, force=force)
        if not moved.exists():
            store.path.rename(moved)
        return requirement

    monkeypatch.setattr(Store, "_check_requirement", move_after_check)
    result = call(
        "advise_ratification",
        {"items": [{"id": draft.id, "verdict": "accept", "reason": "Reviewed."}]},
    )

    assert moved.is_dir()
    assert not (path / "format").exists()
    assert not path.exists()
    assert result.is_error
    assert "no store" in result.content[0].text
    assert server._store is None


@pytest.mark.skipif(os.name == "nt", reason="Windows may prevent renaming an open SQLite index")
def test_advise_does_not_memoize_store_moved_during_index_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".sidegraph"
    with Store(root):
        pass
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server, "_store", None)
    refresh = Store._refresh_freshness

    def move_after_refresh(store: Store) -> None:
        refresh(store)
        root.rename(tmp_path / "moved")

    monkeypatch.setattr(Store, "_refresh_freshness", move_after_refresh)
    result = call("advise_ratification", {"items": []})

    assert result.is_error
    assert "no store" in result.content[0].text
    assert server._store is None
    assert not root.exists()


@pytest.mark.parametrize("layout", ["malformed", "vanishing", "packed"])
def test_advise_cold_open_distinguishes_format_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    root = tmp_path / ".sidegraph"
    with Store(root):
        pass
    marker = root / "format"
    if layout == "malformed":
        marker.write_text("Example project note\n")
    elif layout == "packed":
        marker.unlink()
        shards = root / "shards"
        shards.mkdir()
        (shards / "00.shard").touch()
    else:
        open_path = Path.open

        def remove_marker_before_open(path: Path, *args, **kwargs):
            if path == marker:
                marker.unlink()
            return open_path(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", remove_marker_before_open)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server, "_store", None)

    result = call("advise_ratification", {"items": []})

    assert result.is_error
    if layout == "packed":
        assert "run sidegraph-migrate (or restore the major-2 marker)" in result.content[0].text
        assert not marker.exists()
    else:
        assert result.content[0].text == WRITE_FAILURE
    assert server._store is None


@pytest.mark.parametrize("layout", ["file", "directory", "interrupted"])
def test_advise_cold_open_preserves_legacy_migration_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    path = tmp_path / ".sidegraph"
    if layout == "interrupted":
        staging = tmp_path / ".sidegraph-migrating-abc"
        staging.mkdir()
        _write_sentinel(path, staging)
        expected = "was interrupted"
    else:
        if layout == "directory":
            path.mkdir()
        _legacy_db(path / "decisions.db" if layout == "directory" else path)
        expected = "legacy single-file store (schema_version 0.3.0)"
    before = sorted(tmp_path.rglob("*"))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(path))
    monkeypatch.setattr(server, "_store", None)

    result = call(
        "advise_ratification",
        {"items": [{"id": "draft", "verdict": "accept", "reason": "Reviewed."}]},
    )

    assert result.is_error
    assert expected in result.content[0].text
    assert "Sidegraph 0.23.x" in result.content[0].text
    assert sorted(tmp_path.rglob("*")) == before


def test_tool_classified(store: Store) -> None:
    result = call("advise_ratification", {"items": []})

    assert not result.is_error, result.content
    assert json.loads(result.content[0].text) == {}
    assert server._REGISTERED_CALLABLES["advise_ratification"] is server.advise_ratification
    assert "advise_ratification" in server._WRITE_TOOLS
