"""The drift-check fold planner: which check files pack into ``verifications/packed.shard``,
which are dropped as dead, and the exact image to publish. Pure: it reads nothing and writes
nothing; the caller supplies the occurrences, the dirty set and the decision statuses.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§3, §4, §6.2)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from pydantic import ValidationError

from .drift_checks import CheckOccurrence, check_stem, conflicting_keys, is_packable
from .schema import DecisionStatus, DriftCheck
from .store_format import Inventory, encode_body
from .store_layout import NOT_A_FILE_REASON, PACKED_CHECKS_NAME, VERIFICATIONS_SUBDIR
from .store_rules import effective_all

# Mirrors store._TERMINAL_DECISION_STATUSES by value; store imports maintenance, which will
# import this module, so the set is restated here.
_TERMINAL = frozenset(
    {
        DecisionStatus.SUPERSEDED.value,
        DecisionStatus.REJECTED.value,
        DecisionStatus.DEPRECATED.value,
    }
)

OUTSIDE_GIT = "outside a git work tree"
_PACKED_REL = f"{VERIFICATIONS_SUBDIR}/{PACKED_CHECKS_NAME}"


class DecisionState(NamedTuple):
    """A decision's status class and every store-relative path holding one of its occurrences.
    # see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§6.2)"""

    terminal: bool
    paths: tuple[str, ...]


def decision_states(inv: Inventory) -> dict[str, DecisionState]:
    """The state of every decision with an effective record.
    # see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§6.1)"""
    groups = inv.by_identity()
    states: dict[str, DecisionState] = {}
    for (record_type, record_id), eff in effective_all(inv).items():
        if record_type != "decision" or not isinstance(eff.raw, dict):
            continue
        paths = tuple(occ.path for occ in groups[(record_type, record_id)])
        states[record_id] = DecisionState(eff.raw.get("status") in _TERMINAL, paths)
    return states


@dataclass
class CheckPlan:
    """What a check fold would do. ``read`` holds the exact bytes of every file the plan packs
    or drops, the witnesses the publisher re-reads before each unlink.
    # see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§6.2)"""

    current: bytes | None
    image: bytes
    packed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    dropped_lines: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    blocked: str | None = None
    read: dict[str, bytes] = field(default_factory=dict)

    def has_work(self) -> bool:
        return self.blocked is None and bool(self.packed or self.dropped or self.dropped_lines)


def _valid(occ: CheckOccurrence) -> bool:
    try:
        DriftCheck.model_validate(occ.raw)
    except (ValidationError, ValueError, RecursionError):
        return False
    return True


def _file_reason(
    occ: CheckOccurrence,
    dirty: frozenset[str],
    store_rel: str,
    decisions: dict[str, DecisionState],
) -> str | None:
    """Why a check file stays; ``None`` when it is eligible (section 6.2)."""
    rooted = f"{store_rel}/{occ.path}" if store_rel else occ.path
    if rooted in dirty:
        return "untracked or modified"
    if not occ.selectable:
        return "unselectable"
    if not _valid(occ):
        return "invalid check"
    stem = check_stem(occ.path.rsplit("/", 1)[-1])
    assert isinstance(occ.raw, dict)
    if occ.raw.get("id") != stem:
        return "id differs from file name"
    if not is_packable(occ.raw):
        return "not packable"
    if occ.decision_id not in decisions:
        return "decision missing"
    return None


def plan_check_fold(
    occs: list[CheckOccurrence],
    *,
    dirty: frozenset[str] | None,
    store_rel: str,
    decisions: dict[str, DecisionState],
    packed: bytes | None,
    packed_problem: str | None,
    skipped: tuple[str, ...] = (),
) -> CheckPlan:
    """Plan the fold of ``verifications/*.json`` into ``verifications/packed.shard``. ``dirty``
    holds root-relative paths; ``None`` means outside a git work tree, where every file is kept
    and nothing is dropped. ``skipped`` names check files the inventory did not read because
    they are not regular files; each is kept.
    # see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§6.2)"""
    plan = CheckPlan(current=packed, image=packed or b"")
    files = [occ for occ in occs if occ.kind == "file"]
    lines = [occ for occ in occs if occ.kind == "line"]
    if dirty is None:  # section 6.1 wins over every blocker: outside git nothing is judged
        plan.kept = sorted(
            [*((occ.path, OUTSIDE_GIT) for occ in files), *((rel, OUTSIDE_GIT) for rel in skipped)]
        )
        return plan
    if packed_problem is not None:
        plan.blocked = f"packed.shard is {packed_problem}"
        return plan

    plan.kept = [(rel, NOT_A_FILE_REASON) for rel in skipped]
    eligible: list[CheckOccurrence] = []
    for occ in files:
        reason = _file_reason(occ, dirty, store_rel, decisions)
        if reason is None:
            eligible.append(occ)
        else:
            plan.kept.append((occ.path, reason))

    conflicts = conflicting_keys(occs)
    if conflicts:
        plan.blocked = f"conflicting checks share a key: {', '.join(sorted(conflicts))}"
        return plan

    def terminal_clean(decision_id: str) -> bool:
        """Rule (b): the effective record is terminal and no occurrence of it is dirty."""
        state = decisions.get(decision_id)
        if state is None or not state.terminal:
            return False
        return not any((f"{store_rel}/{p}" if store_rel else p) in dirty for p in state.paths)

    # Candidates for the image: valid, packable lines and eligible files.
    damaged = [
        occ for occ in lines if not (occ.selectable and _valid(occ) and is_packable(occ.raw))
    ]
    damaged_ids = {id(occ) for occ in damaged}
    candidates = [*eligible, *(occ for occ in lines if id(occ) not in damaged_ids)]
    # Only committed check data makes another check dead: while packed.shard is untracked or
    # modified, its lines still carry into the image but supersede nothing.
    shard = f"{store_rel}/{_PACKED_REL}" if store_rel else _PACKED_REL
    superseders = eligible if shard in dirty else candidates
    top: dict[str, str] = {}  # decision id -> highest superseder key (rule (a))
    for occ in superseders:
        assert occ.decision_id is not None and occ.key is not None
        if occ.key > top.get(occ.decision_id, ""):
            top[occ.decision_id] = occ.key

    def dead(occ: CheckOccurrence) -> bool:
        if not occ.selectable:
            return False
        assert occ.decision_id is not None and occ.key is not None
        return terminal_clean(occ.decision_id) or top.get(occ.decision_id, "") > occ.key

    blocking = [occ for occ in damaged if not dead(occ)]
    if blocking:
        first = blocking[0]
        where = f"{first.path}:{first.line_no}" if first.line_no else first.path
        plan.blocked = f"packed.shard has a damaged line that is not dead ({where})"
        return plan

    plan.dropped_lines = sorted({occ.key for occ in lines if dead(occ) and occ.key})
    image: dict[str, object] = {}
    for occ in candidates:
        if dead(occ):
            if occ.kind == "file":
                plan.dropped.append(occ.path)
                plan.read[occ.path] = occ.data
            continue
        assert occ.key is not None
        image.setdefault(occ.key, occ.raw)
        if occ.kind == "file":
            plan.packed.append(occ.path)
            plan.read[occ.path] = occ.data
    plan.packed.sort()
    plan.dropped.sort()
    plan.kept.sort()
    plan.image = b"".join(
        encode_body(image[key]).encode("utf-8") + b"\n"
        for key in sorted(image)  # type: ignore[arg-type]
    )
    return plan
