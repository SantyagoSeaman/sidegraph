"""Drift→supersede wave D4/D5: the SessionStart drift line and the Stop-nudge drift
clause. The refresh itself is monkeypatched (its git mechanics are covered by
``test_sync_drift_cache.py``) — these tests pin the HOOK wiring: when the refresh runs,
what gates each prose surface, and the never-crash / ≤800-bound contracts.
# see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D4/D5)
"""

from __future__ import annotations

import io
import json

import pytest

import sidegraph.host.hooks as hooks
import sidegraph.sync as sync_mod


def _run_session_start(monkeypatch, capsys, db, payload=None):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if payload else ""))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def _run_stop(monkeypatch, capsys, payload, db):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.stop()
    return json.loads(capsys.readouterr().out)


def _user_prompt(text="hello"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text="ok"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _transcript(tmp_path, real_prompts=2, name="s1"):
    # Named after the session, as both hosts do (Claude Code: `<session_id>.jsonl`; Codex:
    # the per-thread rollout file) — hooks._session_identity takes the session from this
    # path, so two sessions sharing one file name would share their per-session ledgers.
    path = tmp_path / f"{name}.jsonl"
    with open(path, "w") as fh:
        for _ in range(real_prompts):
            fh.write(json.dumps(_user_prompt()) + "\n")
            fh.write(json.dumps(_assistant()) + "\n")
    return str(path)


def _fake_refresh(monkeypatch, n):
    calls = []

    def fake(store, **kw):
        calls.append(kw)
        return n

    monkeypatch.setattr(sync_mod, "refresh_code_drift_cache", fake)
    return calls


# -- SessionStart (D4) ----------------------------------------------------------------------


def test_session_start_appends_drift_line(tmp_path, monkeypatch, capsys):
    calls = _fake_refresh(monkeypatch, 3)
    out = _run_session_start(monkeypatch, capsys, tmp_path / ".sidegraph")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "Sidegraph: 3 record(s) are anchored to code that changed after their capture" in text
    assert "[drifted] tag in retrieval" in text
    assert "sidegraph-doctor" in text
    assert len(calls) == 1


def test_session_start_no_line_when_zero_or_none(tmp_path, monkeypatch, capsys):
    for n in (0, None):
        _fake_refresh(monkeypatch, n)
        out = _run_session_start(monkeypatch, capsys, tmp_path / f".sidegraph-{n}")
        text = out["hookSpecificOutput"]["additionalContext"]
        assert "anchored to code that changed" not in text


def test_session_start_kill_switch_suppresses_line_but_still_refreshes(
    tmp_path, monkeypatch, capsys
):
    """SIDEGRAPH_DRIFT_NUDGE=off gates the PROSE only (spec I5, option b): the refresh
    still runs so D3's markers stay fresh — a frozen cache with live markers was the
    rev-1 defect this rule exists to prevent."""
    monkeypatch.setenv("SIDEGRAPH_DRIFT_NUDGE", "off")
    calls = _fake_refresh(monkeypatch, 3)
    out = _run_session_start(monkeypatch, capsys, tmp_path / ".sidegraph")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "anchored to code that changed" not in text
    assert len(calls) == 1


def test_session_start_duplicate_call_makes_no_refresh(tmp_path, monkeypatch, capsys):
    """The D7.2 dedupe early-exit fires BEFORE the drift block — a duplicate SessionStart
    for the same session re-runs no scan (review pt 11: one refresh per logical
    session)."""
    calls = _fake_refresh(monkeypatch, 1)
    payload = {"session_id": "dup-1"}
    _run_session_start(monkeypatch, capsys, tmp_path / ".sidegraph", payload)
    assert len(calls) == 1
    out = _run_session_start(monkeypatch, capsys, tmp_path / ".sidegraph", payload)
    assert out == {}  # dedupe exit
    assert len(calls) == 1  # no second refresh


def test_session_start_survives_refresh_raising(tmp_path, monkeypatch, capsys):
    def boom(store, **kw):
        raise RuntimeError("refresh exploded")

    monkeypatch.setattr(sync_mod, "refresh_code_drift_cache", boom)
    out = _run_session_start(monkeypatch, capsys, tmp_path / ".sidegraph")
    # The map is this hook's deliverable — it must render regardless.
    assert "hookSpecificOutput" in out


# -- Stop (D5) ------------------------------------------------------------------------------


def _stop_payload(tmp_path, session="s1"):
    return {"session_id": session, "transcript_path": _transcript(tmp_path, name=session)}


@pytest.mark.parametrize("n", [5, 10**6])
def test_stop_appends_clause_within_bound(tmp_path, monkeypatch, capsys, n):
    """Review M-6: the bound is exercised beyond the measured n=5 — a million drifted
    records still fits (the clause only grows by digits of n; it breaks around a
    16-digit count, unreachable)."""
    _fake_refresh(monkeypatch, n)
    out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path), tmp_path / ".sidegraph")
    assert out["decision"] == "block"
    assert out["reason"].startswith(hooks.CAPTURE_NUDGE)
    assert f"Also: {n} drifted record(s)" in out["reason"]
    assert "supersede_decision" in out["reason"]
    # The prior wave's pinned anti-creep bound (staleness-machinery D3) holds over the
    # CONCATENATED emission — spec C1.
    assert len(out["reason"]) <= 800


def test_stop_plain_nudge_when_no_drift(tmp_path, monkeypatch, capsys):
    for n in (0, None):
        _fake_refresh(monkeypatch, n)
        out = _run_stop(
            monkeypatch, capsys, _stop_payload(tmp_path, session=f"s-{n}"), tmp_path / ".sg"
        )
        assert out["decision"] == "block"
        assert out["reason"] == hooks.CAPTURE_NUDGE


def test_stop_kill_switch_suppresses_clause_but_still_refreshes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIDEGRAPH_DRIFT_NUDGE", "off")
    calls = _fake_refresh(monkeypatch, 5)
    out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path), tmp_path / ".sidegraph")
    assert out["reason"] == hooks.CAPTURE_NUDGE
    assert len(calls) == 1


def test_stop_refresh_failure_still_nudges(tmp_path, monkeypatch, capsys):
    def boom(store, **kw):
        raise RuntimeError("refresh exploded")

    monkeypatch.setattr(sync_mod, "refresh_code_drift_cache", boom)
    out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path), tmp_path / ".sidegraph")
    assert out["decision"] == "block"
    assert out["reason"] == hooks.CAPTURE_NUDGE


def test_stop_refresh_runs_after_substance_gate_only(tmp_path, monkeypatch, capsys):
    """ONE test, both halves (spec M5): a gated (not-substantial) session makes ZERO
    refresh calls — no git work on trivial sessions — and a substantial one makes ≥1.
    The ≥1 half is the honest red half against unfixed code."""
    calls = _fake_refresh(monkeypatch, 1)
    gated = {"session_id": "s-gated", "transcript_path": _transcript(tmp_path, 1, "s-gated")}
    out = _run_stop(monkeypatch, capsys, gated, tmp_path / ".sidegraph")
    assert out == {}
    assert len(calls) == 0

    substantial = {"session_id": "s-full", "transcript_path": _transcript(tmp_path, name="s-full")}
    out = _run_stop(monkeypatch, capsys, substantial, tmp_path / ".sidegraph")
    assert out["decision"] == "block"
    assert len(calls) == 1
