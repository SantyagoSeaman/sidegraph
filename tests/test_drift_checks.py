"""Attributed scoped drift checks preserve capture history and re-arm on later changes."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from sidegraph import server
from sidegraph.doctor import scan_code_drift
from sidegraph.retrieval import drifted_record_ids
from sidegraph.schema import AnchorBinding, Descriptor, Entity, Provenance
from sidegraph.store import Store
from sidegraph.sync import refresh_code_drift_cache
from tests.drift_checks_support import (  # noqa: F401
    drift_ids,
    git,
    project,
    record_check,
    require_check,
)

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)


def test_wire_check_preserves_original_decision_and_clears_only_its_advisory(project):
    root, store, decision = project
    original = (store.path / "decisions" / f"{decision.id}.json").read_bytes()
    assert drift_ids(store) == {decision.id}
    assert refresh_code_drift_cache(store) == 1
    assert drifted_record_ids(store) == {decision.id}

    result = record_check(root, decision.id)
    assert not result.is_error, result.content
    checks = list((store.path / "verifications").glob("*.json"))
    assert len(checks) == 1
    payload = json.loads(checks[0].read_text(encoding="utf-8"))
    assert payload["decision_id"] == decision.id
    assert payload["checked_commit"] == git(root, "rev-parse", "HEAD")
    assert payload["provenance"]["source"] == "agent"
    assert (store.path / "decisions" / f"{decision.id}.json").read_bytes() == original
    assert drift_ids(store) == set()
    assert drifted_record_ids(store) == set()


@pytest.mark.parametrize(
    "overrides",
    [
        {"checked_commit": "0" * 40},
        {"files": []},
        {"files": ["../private-scope-canary"]},
        {"rationale": ""},
    ],
)
def test_invalid_check_has_a_safe_validation_error_without_publication(project, overrides):
    root, store, decision = project
    before = {p.relative_to(store.path): p.read_bytes() for p in store.path.rglob("*.json")}
    result = record_check(root, decision.id, **overrides)
    assert result.is_error
    text = result.content[0].text
    assert text.startswith("invalid draft:"), text
    assert "private-scope-canary" not in text
    assert before == {p.relative_to(store.path): p.read_bytes() for p in store.path.rglob("*.json")}


def commit_file(root, name, content):
    (root / name).write_text(content, encoding="utf-8")
    git(root, "add", "--", name)
    git(root, "commit", "-q", "-m", "synthetic source change")


def test_later_checked_change_rearms_and_unrelated_commit_does_not(project):
    root, store, decision = project
    check = require_check(root, decision.id)
    commit_file(root, "unrelated.py", "unrelated = True\n")
    assert drift_ids(store) == set()
    assert refresh_code_drift_cache(store) == 0
    commit_file(root, "code.py", "def policy():\n    return 2\n")
    assert drift_ids(store) == {decision.id}
    assert refresh_code_drift_cache(store) == 1
    from sidegraph.doctor import CODE_DRIFT, curate

    finding = next(f for f in curate(store.path).findings if f.code == CODE_DRIFT)
    assert "checked files changed" in finding.detail
    assert check["id"] in finding.detail
    assert check["checked_commit"][:8] in finding.detail


@pytest.mark.parametrize("index_flag", [None, "--assume-unchanged"])
def test_dirty_inspected_source_is_refused_even_behind_index_flags(project, index_flag):
    root, store, decision = project
    if index_flag:
        git(root, "update-index", index_flag, "code.py")
    (root / "code.py").write_text("def policy():\n    return 2\n", encoding="utf-8")
    result = record_check(root, decision.id)
    assert result.is_error
    assert result.content[0].text.startswith("invalid draft: files:")
    assert not (store.path / "verifications").exists()


def test_unrelated_dirty_file_does_not_block_an_explicit_check(project):
    root, store, decision = project
    (root / "unrelated.py").write_text("not_committed = True\n", encoding="utf-8")
    require_check(root, decision.id)
    assert drift_ids(store) == set()


def test_deleted_inspected_dependency_rearms_even_without_capture_anchor_delta(project):
    root, store, decision = project
    # Return the anchor to capture bytes before checking, with an extra inspected dependency.
    commit_file(root, "code.py", "def policy():\n    return 1\n")
    commit_file(root, "dependency.py", "dependency = 1\n")
    require_check(root, decision.id, files=["code.py", "dependency.py"])
    assert drift_ids(store) == set()
    git(root, "rm", "dependency.py")
    git(root, "commit", "-q", "-m", "remove dependency")
    entries = [entry for batch in scan_code_drift(store.path).batches for entry in batch.entries]
    assert len(entries) == 1
    assert entries[0].record_id == decision.id
    assert entries[0].files == ["dependency.py"]


def orphan(store, decision, name, file_path):
    entity = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=file_path))
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="orphaned")
    )
    return entity


def restore(store, decision, entity):
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status="live")
    )


def test_same_head_dependency_becoming_required_rearms_covered_but_changed_scope(project):
    root, store, decision = project
    commit_file(root, "dependency.py", "dependency = 1\n")
    entity = orphan(store, decision, "dependency.py", "dependency.py")
    require_check(root, decision.id, files=["code.py", "dependency.py"])
    head = git(root, "rev-parse", "HEAD")
    assert refresh_code_drift_cache(store) == 0
    restore(store, decision, entity)
    assert git(root, "rev-parse", "HEAD") == head
    assert drift_ids(store) == {decision.id}
    assert refresh_code_drift_cache(store) == 1


def test_same_file_restoration_keeps_explicit_whole_file_coverage(project):
    root, store, decision = project
    entity = orphan(store, decision, "code.py", "code.py")
    require_check(root, decision.id)
    restore(store, decision, entity)
    assert drift_ids(store) == set()


def test_changed_descriptor_invalidates_the_checked_subject(project):
    root, store, decision = project
    require_check(root, decision.id)
    entity = store.get_entity(store.bindings_for_record(decision.id)[0].entity_id)
    store.upsert_entity(
        entity.model_copy(
            update={"descriptor": Descriptor(name="policy_changed", file_path="code.py")}
        )
    )
    assert drift_ids(store) == {decision.id}


def test_check_does_not_change_shared_digest_or_reset_binding_state(project):
    root, store, decision = project
    store.set_meta("volatile_stale", "0")
    before = store._compute_canonical_digest()[0]
    require_check(root, decision.id)
    assert store._compute_canonical_digest()[0] == before
    store.refresh_if_stale()
    assert store.get_meta("volatile_stale") == "0"
    with Store(store.path) as other:
        assert other.get_meta("volatile_stale") == "0"
        assert drift_ids(other) == set()


def test_redaction_precedes_durable_check_storage(project):
    root, store, decision = project
    canary = "synthetic_scope_secret_canary_0123456789"
    require_check(root, decision.id, rationale=f"Still pure. api_key={canary}")
    payload = next((store.path / "verifications").glob("*.json")).read_text(encoding="utf-8")
    assert canary not in payload
    assert "Still pure." in payload


def check_path(store):
    return next((store.path / "verifications").glob("*.json"))


@pytest.mark.parametrize("defect", ["schema", "filename", "target", "actor", "revision"])
def test_snapshot_lints_auxiliary_history_without_trusting_its_applicability(project, defect):
    from sidegraph.verify import verify_snapshot

    root, store, decision = project
    require_check(root, decision.id)
    path = check_path(store)
    assert verify_snapshot(store.path) == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = "parse-error"
    if defect == "schema":
        payload["schema_version"] = 2
    elif defect == "filename":
        path.rename(path.with_name("0" * 26 + ".json"))
        expected = "filename-id-mismatch"
    elif defect == "target":
        payload["decision_id"] = "missing-decision"
        expected = "dangling-drift-check"
    elif defect == "actor":
        payload["provenance"]["source"] = "manual"
    else:
        payload["provenance"]["commit"] = "0" * 40
    if defect != "filename":
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    findings = verify_snapshot(store.path)
    assert expected in {finding.code for finding in findings}


@pytest.mark.parametrize("operation", ["edit", "reformat", "delete"])
def test_transition_rejects_every_change_to_published_check_bytes(project, operation):
    from sidegraph.verify import verify_against

    root, store, decision = project
    require_check(root, decision.id)
    git(root, "add", ".sidegraph")
    git(root, "commit", "-q", "-m", "publish synthetic check")
    path = check_path(store)
    if operation == "delete":
        path.unlink()
    elif operation == "reformat":
        path.write_text(json.dumps(json.loads(path.read_text())) + "\n", encoding="utf-8")
    else:
        payload = json.loads(path.read_text())
        payload["rationale"] = "Replacement is forbidden."
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    findings = verify_against(store.path, "HEAD")
    assert len(findings) == 1
    assert findings[0].code == (
        "illegal-deletion" if operation == "delete" else "illegal-field-change"
    )
    assert "/verifications/" in findings[0].path


def test_auxiliary_history_is_reported_as_uncommitted_store_work(project):
    from datetime import timedelta

    from sidegraph.integrity import Inputs, _detect_store_uncommitted

    root, store, decision = project
    git(root, "add", ".sidegraph")
    git(root, "commit", "-q", "-m", "publish synthetic store")
    require_check(root, decision.id)
    problem = _detect_store_uncommitted(
        Inputs(store_dir=store.path, now=datetime.now(UTC) + timedelta(days=2))
    )
    assert problem is not None
    assert problem.summary == "1 store file(s) uncommitted"


def test_optional_comparison_failure_preserves_other_capture_advisories(project, monkeypatch):
    from sidegraph import drift_checks

    root, store, decision = project
    other = store.add_decision(decision.model_copy(update={"id": "other", "title": "Other rule"}))
    for binding in store.bindings_for_record(decision.id):
        store.add_binding(binding.model_copy(update={"record_id": other.id}))
    require_check(root, decision.id)

    def unavailable(*args, **kwargs):
        raise ValueError("synthetic Git failure")

    monkeypatch.setattr(drift_checks, "tree_entries", unavailable)
    scan = scan_code_drift(store.path)
    assert scan.check_skipped is True
    assert not any(batch.failed for batch in scan.batches)
    assert {entry.record_id for batch in scan.batches for entry in batch.entries} == {
        decision.id,
        other.id,
    }
    # A fresh cache must keep both capture flags; this is not only a warm-cache fallback.
    store.delete_meta("code_drift_cache")
    assert refresh_code_drift_cache(store) == 2
    assert drifted_record_ids(store) == {decision.id, other.id}


def test_check_publication_is_exclusive_and_keeps_history(project):
    from sidegraph.schema import DriftCheck

    root, store, decision = project
    require_check(root, decision.id)
    path = check_path(store)
    before = path.read_bytes()
    check = DriftCheck.model_validate_json(before)
    with pytest.raises(FileExistsError):
        store.record_drift_check(check.model_copy(update={"rationale": "Forbidden overwrite"}))
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))
    assert store._conn.in_transaction is False
    require_check(
        root, decision.id, rationale="Second explicit inspection; same scope still holds."
    )
    assert len(list(path.parent.glob("*.json"))) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("state", ["unstamped", "terminal", "abstract-only"])
def test_noncheckable_subject_is_refused_without_backfill(project, state):
    root, store, decision = project
    payload = decision.model_dump(mode="json")
    path = store.path / "decisions" / f"{decision.id}.json"
    if state == "unstamped":
        payload["provenance"]["commit"] = None
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif state == "terminal":
        from ulid import ULID

        store.add_decision(
            decision.model_copy(update={"id": str(ULID()), "supersedes": decision.id})
        )
    else:
        (store.path / "bindings" / f"{decision.id}.json").write_text("[]\n", encoding="utf-8")
    store.refresh_if_stale()
    original = path.read_bytes()
    result = record_check(root, decision.id)
    assert result.is_error
    assert path.read_bytes() == original
    assert not (store.path / "verifications").exists()


def test_executable_mode_change_rearms_check(project):
    root, store, decision = project
    require_check(root, decision.id)
    git(root, "update-index", "--chmod=+x", "code.py")
    git(root, "commit", "-q", "-m", "change source mode")
    assert drift_ids(store) == {decision.id}


def test_real_supersession_and_compaction_preserve_check_history(project):
    from ulid import ULID

    from sidegraph.verify import verify_snapshot

    root, store, decision = project
    require_check(root, decision.id)
    path = check_path(store)
    original = path.read_bytes()
    successor = store.add_decision(
        decision.model_copy(
            update={
                "id": str(ULID()),
                "title": "Actual new policy",
                "choice": "Return two.",
                "supersedes": decision.id,
            }
        )
    )
    assert store.get_decision(decision.id).status == "superseded"
    store.compact()
    assert not (store.path / "decisions" / f"{decision.id}.json").exists()
    assert store.get_decision(successor.id).status == "accepted"
    assert path.read_bytes() == original
    assert verify_snapshot(store.path) == []
    assert drift_ids(store) == set()


def test_checks_share_one_tree_lookup_over_inspected_and_candidate_paths(project, monkeypatch):
    from sidegraph import drift_checks

    root, store, decision = project
    require_check(root, decision.id)
    other = store.add_decision(decision.model_copy(update={"id": "other", "title": "Other rule"}))
    for binding in store.bindings_for_record(decision.id):
        store.add_binding(binding.model_copy(update={"record_id": other.id}))
    commit_file(root, "dependency.py", "dependency = 1\n")
    require_check(root, other.id, files=["code.py", "dependency.py"])
    calls = []
    original = drift_checks.tree_entries

    def tracked(root, head, paths, timeout):
        calls.append(set(paths))
        return original(root, head, paths, timeout)

    monkeypatch.setattr(drift_checks, "tree_entries", tracked)
    assert drift_ids(store) == set()
    assert calls == [{"code.py", "dependency.py"}]


def test_tree_equivalence_survives_missing_checked_commit_after_squash_clone(project, tmp_path):
    root, store, decision = project
    check = require_check(root, decision.id)
    git(root, "add", ".sidegraph")
    git(root, "commit", "-q", "-m", "publish checked state")
    # Collapse all post-capture commits, so original capture remains available but the
    # check's former HEAD is not reachable in the new single-branch clone.
    git(root, "reset", "--soft", decision.provenance.commit)
    git(root, "commit", "-q", "-m", "squashed implementation and check")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--no-local", "--single-branch", root.as_uri(), str(clone))
    absent = subprocess.run(
        ["git", "cat-file", "-e", check["checked_commit"]], cwd=clone, capture_output=True
    )
    assert absent.returncode != 0
    with Store(clone / ".sidegraph") as other:
        assert drift_ids(other) == set()


def test_cache_prune_retains_other_ids_and_original_capture_groups(project):
    root, store, decision = project
    before = {
        "head": "observed-head",
        "computed_at": "prior",
        "by_commit": {
            "capture-a": [decision.id, "keep-a"],
            "capture-b": ["keep-b", decision.id],
        },
    }
    store.set_meta("code_drift_cache", json.dumps(before))
    result = require_check(root, decision.id)
    assert result["cache_cleared"] is True
    assert json.loads(store.get_meta("code_drift_cache")) == {
        **before,
        "by_commit": {"capture-a": ["keep-a"], "capture-b": ["keep-b"]},
    }


@pytest.mark.parametrize("index_flag", [None, "--skip-worktree"])
def test_dirty_executable_mode_is_refused_even_behind_index_flags(project, index_flag):
    root, store, decision = project
    if git(root, "config", "--bool", "core.filemode") != "true":
        pytest.skip("filesystem does not track executable modes")
    if index_flag:
        git(root, "update-index", index_flag, "code.py")
    (root / "code.py").chmod(0o755)
    result = record_check(root, decision.id)
    assert result.is_error
    assert result.content[0].text.startswith("invalid draft: files:")
    assert not (store.path / "verifications").exists()


def test_nontracking_filemode_configuration_allows_platform_mode_difference(project):
    root, store, decision = project
    git(root, "config", "core.filemode", "false")
    (root / "code.py").chmod(0o755)
    require_check(root, decision.id)
    assert drift_ids(store) == set()


def test_wrong_semantic_assertion_stays_history_after_real_correction(project):
    from ulid import ULID

    root, store, decision = project
    commit_file(root, "code.py", "def policy():\n    return 2\n")
    # A mechanical snapshot cannot detect that this supplied judgment is wrong.
    require_check(root, decision.id, rationale="Synthetic mistaken judgment: still returns one.")
    assert drift_ids(store) == set()
    path = check_path(store)
    original = path.read_bytes()
    successor = store.add_decision(
        decision.model_copy(
            update={
                "id": str(ULID()),
                "title": "Corrected actual policy",
                "choice": "Policy returns two.",
                "supersedes": decision.id,
                "provenance": Provenance(source="manual", commit=git(root, "rev-parse", "HEAD")),
            }
        )
    )
    assert successor.choice == "Policy returns two."
    assert store.get_decision(decision.id).status == "superseded"
    assert path.read_bytes() == original


def test_symlinked_store_checks_owning_project_not_physical_store_repository(
    project, tmp_path, monkeypatch
):
    root, store, decision = project
    foreign = tmp_path / "foreign-repository"
    foreign.mkdir()
    git(foreign, "init", "-q")
    git(foreign, "config", "user.name", "Synthetic Foreign")
    git(foreign, "config", "user.email", "fixture@example.invalid")
    commit_file(foreign, "code.py", "unrelated = 3\n")
    original_path = store.path
    store.close()
    original_path.rename(foreign / "physical-store")
    original_path.symlink_to(foreign / "physical-store", target_is_directory=True)
    with Store(original_path) as linked:
        monkeypatch.setattr(server, "_store", linked)
        result = require_check(root, decision.id)
        assert result["checked_commit"] == git(root, "rev-parse", "HEAD")
        assert result["checked_commit"] != git(foreign, "rev-parse", "HEAD")
        assert drift_ids(linked) == set()


def test_uncovered_anchor_delta_is_retained_after_scoped_check(project):
    root, store, decision = project
    (root / "alias.py").symlink_to("code.py")
    git(root, "add", "alias.py")
    git(root, "commit", "-q", "-m", "add symlink anchor")
    entity = store.upsert_entity(
        Entity(
            canonical_name="alias.py", descriptor=Descriptor(name="alias.py", file_path="alias.py")
        )
    )
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))
    refresh_code_drift_cache(store)
    result = require_check(root, decision.id)
    assert result["cache_cleared"] is False
    assert drifted_record_ids(store) == {decision.id}
    assert drift_ids(store) == {decision.id}
    (root / "alias.py").unlink()
    (root / "alias.py").symlink_to("other.py")
    git(root, "add", "alias.py")
    git(root, "commit", "-q", "-m", "retarget uncovered anchor")
    assert drift_ids(store) == {decision.id}


def test_invalid_latest_notice_does_not_label_capture_as_checked_revision(project):
    from sidegraph.doctor import CODE_DRIFT, curate

    root, store, decision = project
    require_check(root, decision.id)
    path = check_path(store)
    payload = json.loads(path.read_text())
    payload.pop("rationale")
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    detail = next(f.detail for f in curate(store.path).findings if f.code == CODE_DRIFT)
    assert f"capture {decision.provenance.commit[:8]}" in detail
    assert f"revision {decision.provenance.commit[:8]}" not in detail


def test_failed_capture_group_with_check_on_empty_cache_stays_unknown(project, monkeypatch):
    from sidegraph import doctor, drift_checks

    root, store, decision = project
    require_check(root, decision.id)
    store.delete_meta("code_drift_cache")

    def unavailable(*args, **kwargs):
        raise ValueError("synthetic unavailable capture")

    def forbidden(*args, **kwargs):
        pytest.fail("optional comparison must not run over a failed capture group")

    monkeypatch.setattr(doctor, "_git_diff_name_only", unavailable)
    monkeypatch.setattr(drift_checks, "tree_entries", forbidden)
    scan = scan_code_drift(store.path)
    assert len(scan.batches) == 1 and scan.batches[0].failed
    assert refresh_code_drift_cache(store) == 0
    assert any("check skipped" in f.detail for f in doctor.curate(store.path).findings)


def test_unpublished_auxiliary_temp_is_not_reported_for_commit(project):
    from datetime import timedelta

    from sidegraph.integrity import Inputs, _detect_store_uncommitted

    root, store, decision = project
    git(root, "add", ".sidegraph")
    git(root, "commit", "-q", "-m", "publish synthetic store")
    folder = store.path / "verifications"
    folder.mkdir()
    (folder / "unpublished.json.123.abcdef.tmp").write_text("{}\n", encoding="utf-8")
    assert (
        _detect_store_uncommitted(
            Inputs(store_dir=store.path, now=datetime.now(UTC) + timedelta(days=2))
        )
        is None
    )
