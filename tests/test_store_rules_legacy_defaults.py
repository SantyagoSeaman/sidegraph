"""A field whose schema default is non-empty, missing from a legacy file: the writer's
backfill of exactly that default is not a change; any other value still is.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from sidegraph.migrate import migrate
from sidegraph.schema import Decision, Domain, Entity, Fact
from sidegraph.store import Store
from sidegraph.store_layout import hot_matches_archive
from sidegraph.store_rules import (
    DECISION_MUTABLE_FIELDS,
    DOMAIN_MUTABLE_FIELDS,
    ENTITY_MUTABLE_FIELDS,
    FACT_MUTABLE_FIELDS,
    LEGACY_DEFAULTS,
    classify_transition,
    deletable,
)
from sidegraph.verify import verify_against, verify_snapshot
from tests.store_format2_support import (
    OLD,
    commit_all,
    decision_body,
    entity_body,
    format1_committed,
    git_repo,  # noqa: F401  (pytest fixture)
    put_archive,
    put_file,
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _codes(kind: str, old: dict, new: dict) -> list[str]:
    return [f"{v.code}: {v.detail}" for v in classify_transition(kind, old, new)]


def test_absent_scope_backfilled_as_repo_on_ratify_is_legal():
    old = decision_body("D1")
    new = decision_body("D1", status="accepted", scope="repo")
    assert _codes("decision", old, new) == []


@pytest.mark.parametrize(
    ("old_over", "new_over"),
    [
        ({}, {"scope": "global"}),  # absent -> a non-default value
        ({"scope": "repo"}, {"scope": "global"}),  # a real edit
        ({"scope": "global"}, {"scope": "repo"}),  # a real edit back to the default
        ({"scope": "global"}, {}),  # a non-default value removed
    ],
)
def test_real_scope_changes_stay_illegal(old_over, new_over):
    old = decision_body("D1", **old_over)
    new = decision_body("D1", status="accepted")
    new.pop("scope", None)
    new.update(new_over)
    assert _codes("decision", old, new) == ["illegal-field-change: field 'scope' changed"]


def test_an_older_copy_without_scope_beside_a_backfilled_one_is_legal():
    """The other direction: a backfilled body meets an older copy again (a merge, a shard line
    beside a hot file); the reader loads both as ``repo`` (pre-release panel, Muse 1)."""
    old = decision_body("D1", status="accepted", scope="repo")
    new = decision_body("D1", status="accepted")
    assert _codes("decision", old, new) == []


def test_absent_entity_kind_backfilled_as_concrete_is_legal():
    old = entity_body("E1")
    old.pop("kind", None)
    assert _codes("entity", old, {**old, "kind": "concrete"}) == []
    assert _codes("entity", old, {**old, "kind": "abstract"}) == [
        "illegal-field-change: field 'kind' changed"
    ]


def _non_empty_defaults(model: type[BaseModel], mutable: frozenset[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for name, info in model.model_fields.items():
        if name in mutable or info.is_required() or info.default_factory is not None:
            continue
        default = info.default
        if default is None or default == [] or default == {}:
            continue
        out[name] = getattr(default, "value", default)
    return out


@pytest.mark.parametrize(
    ("kind", "model", "mutable"),
    [
        ("decision", Decision, DECISION_MUTABLE_FIELDS),
        ("fact", Fact, FACT_MUTABLE_FIELDS),
        ("domain", Domain, DOMAIN_MUTABLE_FIELDS),
        ("entity", Entity, ENTITY_MUTABLE_FIELDS),
    ],
)
def test_legacy_defaults_match_the_schema(kind, model, mutable):
    assert LEGACY_DEFAULTS.get(kind, {}) == _non_empty_defaults(model, mutable)


def _migrate_and_ratify_legacy_d1(repo):
    store = format1_committed(repo)
    assert "scope" not in read_json(store / "decisions" / "D1.json")
    migrate(store, dry_run=False, any_branch=True)
    commit_all(repo, "migrate", date=OLD)
    s = Store(store)
    s.ratify("D1")
    s.close()
    assert read_json(store / "decisions" / "D1.json")["scope"] == "repo"
    return store


def test_ratified_legacy_decision_verifies_and_packs(git_repo):
    store = _migrate_and_ratify_legacy_d1(git_repo)
    commit_all(git_repo, "ratify", date=OLD)
    assert verify_snapshot(store) == []
    report = Store(store).maintain(settled_days=0, dry_run=False, any_branch=True)
    assert report.skipped == []
    assert "decisions/D1.json" in report.deleted_files
    assert verify_snapshot(store) == []


def test_a_real_scope_edit_of_a_packed_legacy_decision_still_blocks_packing(git_repo):
    store = _migrate_and_ratify_legacy_d1(git_repo)
    path = store / "decisions" / "D1.json"
    put_file(store, "decision", {**read_json(path), "scope": "global"})
    commit_all(git_repo, "edit scope", date=OLD)
    assert [v.code for v in verify_snapshot(store)] == ["shadow-divergence"]
    report = Store(store).maintain(settled_days=0, dry_run=False, any_branch=True)
    assert [(s.record_id, s.code) for s in report.skipped] == [("D1", "shadow-divergence")]


def _legacy_rejected_d9(repo):
    """A format-1 store holding D9, a decision dropped by a version older than ``scope``."""
    store = format1_committed(repo)
    put_file(
        store,
        "decision",
        decision_body("D9", status="rejected", valid_to="2026-01-02T00:00:00Z"),
    )
    commit_all(repo, "legacy D9", date=OLD)
    assert "scope" not in read_json(store / "decisions" / "D9.json")
    return store


def test_hot_matches_archive_takes_the_kind_defaults_at_top_level_only():
    defaults = LEGACY_DEFAULTS["decision"]
    hot = decision_body("D9")
    archived = {**hot, "scope": "repo"}
    assert not hot_matches_archive(hot, archived)
    assert hot_matches_archive(hot, archived, defaults)
    assert not hot_matches_archive(hot, {**hot, "scope": "global"}, defaults)
    assert not hot_matches_archive(
        {**hot, "scope": "global"}, archived, LEGACY_DEFAULTS["decision"]
    )
    nested_hot = {"provenance": {"source": "manual"}}
    nested_archived = {"provenance": {"source": "manual", "scope": "repo"}}
    assert not hot_matches_archive(nested_hot, nested_archived, defaults)


def test_format1_compact_removes_a_legacy_hot_file_and_verifies(git_repo):
    store = _legacy_rejected_d9(git_repo)
    s = Store(store)
    report = s.compact()
    s.close()
    assert not (store / "decisions" / "D9.json").exists()
    assert report.kept_hot_files == 0
    assert verify_snapshot(store) == []


def test_a_kept_legacy_hot_file_beside_its_archive_line_packs_after_migration(git_repo):
    """A store compacted by an earlier version kept D9's hot file beside the archive line
    that holds ``scope: repo``; maintenance now folds the pair."""
    store = _legacy_rejected_d9(git_repo)
    hot = read_json(store / "decisions" / "D9.json")
    put_archive(store, "0001", [{**hot, "scope": "repo", "record_type": "decision"}])
    commit_all(git_repo, "earlier compact kept D9", date=OLD)
    migrate(store, dry_run=False, any_branch=True)
    commit_all(git_repo, "migrate", date=OLD)
    report = Store(store).maintain(settled_days=0, dry_run=False, any_branch=True)
    assert report.skipped == []
    assert report.kept == []
    assert not (store / "decisions" / "D9.json").exists()
    assert not (store / "archive" / "0001.jsonl").exists()
    assert verify_snapshot(store) == []


def test_deletable_takes_the_kind_defaults_only_for_a_key_the_shard_lacks():
    legacy = decision_body("D9", status="rejected")
    line = {**legacy, "scope": "repo"}
    assert deletable("decision", line, legacy)
    assert not deletable("decision", {**legacy, "scope": "global"}, legacy)
    assert not deletable("fact", {**legacy, "scope": "repo"}, legacy)
    assert not deletable("decision", {**line, "note": None}, legacy)  # a null key is still a key


def test_maintenance_retiring_a_backfilled_archive_copy_passes_verify_against(git_repo):
    """Maintenance deletes an archive line whose only extra key is the backfilled default
    (``deletable``); ``verify --against`` must judge that removal the same way, not as a lost
    raw key (pre-release panel, Sol 1)."""
    from sidegraph.verify import verify_against
    from tests.store_format2_support import make_store, put_shard_line

    store = make_store(git_repo, fmt=2)
    legacy = decision_body("D9", status="rejected", valid_to="2026-01-02T00:00:00Z")
    put_shard_line(store, "decision", "D9", legacy)
    put_archive(store, "0001", [{**legacy, "scope": "repo", "record_type": "decision"}])
    commit_all(git_repo, "packed legacy D9 beside a backfilled archive copy", date=OLD)
    report = Store(store).maintain(settled_days=0, dry_run=False, any_branch=True)
    assert not (store / "archive" / "0001.jsonl").exists(), report
    assert verify_against(store, "HEAD") == []


@pytest.mark.parametrize("kind", ["decision", "entity"])
def test_a_hot_file_without_the_default_beside_a_backfilled_shard_line_folds(git_repo, kind):
    """Pre-release panel, Muse 1 (divergence) and Sol r3 (maintenance kept the file because
    "keys would be lost"): the older hot copy folds onto the backfilled shard line."""
    from tests.store_format2_support import make_store, put_shard_line

    store = make_store(git_repo, fmt=2)
    if kind == "decision":
        body, key, default = decision_body("D1", status="accepted"), "scope", "repo"
    else:
        body, key, default = entity_body("D1"), "kind", "concrete"
        body.pop("kind")
    put_shard_line(store, kind, "D1", {**body, key: default})
    hot = put_file(store, kind, body)
    commit_all(git_repo, "older hot copy beside a backfilled shard line", date=OLD)
    assert verify_snapshot(store) == []
    report = Store(store).maintain(settled_days=0, dry_run=False, any_branch=True)
    assert report.skipped == [] and report.kept == []
    assert not hot.exists()
    assert verify_against(store, "HEAD") == []


@pytest.mark.parametrize("kind", ["decision", "entity"])
def test_deleting_an_older_hot_copy_by_hand_beside_its_backfilled_shard_line(git_repo, kind):
    """Pre-release panel, Grok r3: removing the hot file by hand is the same fold maintenance
    performs, so --against must accept it."""
    from tests.store_format2_support import make_store, put_shard_line

    store = make_store(git_repo, fmt=2)
    if kind == "decision":
        body, key, default = decision_body("D1", status="accepted"), "scope", "repo"
    else:
        body, key, default = entity_body("D1"), "kind", "concrete"
        body.pop("kind")
    put_shard_line(store, kind, "D1", {**body, key: default})
    hot = put_file(store, kind, body)
    commit_all(git_repo, "older hot copy beside a backfilled shard line", date=OLD)
    hot.unlink()
    assert verify_against(store, "HEAD") == []


def test_a_real_scope_value_beside_a_legacy_archive_copy_is_not_compatible():
    """Pre-release panel, Sol r2 (HIGH): projecting the shard body onto the archive's keys
    dropped ``scope``, so ``global`` beside a legacy (``repo``) archive copy read as compatible
    and the archive copy could be deleted."""
    from sidegraph.store_rules import compatible

    legacy = decision_body("D9", status="rejected", valid_to="2026-01-02T00:00:00Z")
    assert not compatible("decision", legacy, {**legacy, "scope": "global"})
    assert not deletable("decision", legacy, {**legacy, "scope": "global"})
    assert compatible("decision", legacy, {**legacy, "scope": "repo"})


def test_maintenance_keeps_a_legacy_archive_copy_beside_a_different_scope(git_repo):
    from sidegraph.verify import verify_snapshot as snapshot
    from tests.store_format2_support import make_store, put_shard_line

    store = make_store(git_repo, fmt=2)
    legacy = decision_body("D9", status="rejected", valid_to="2026-01-02T00:00:00Z")
    put_shard_line(store, "decision", "D9", {**legacy, "scope": "global"})
    put_archive(store, "0001", [{**legacy, "record_type": "decision"}])
    commit_all(git_repo, "legacy archive copy beside a global shard line", date=OLD)
    assert snapshot(store) != []
    Store(store).maintain(settled_days=0, dry_run=False, any_branch=True)
    assert (store / "archive" / "0001.jsonl").exists()
