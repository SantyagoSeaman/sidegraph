"""The §8 merge matrix (T-G1): real ``git merge-tree --write-tree`` in a BARE clone on packed
(format 2) stores, then ``sidegraph-verify`` (snapshot and ``--against``) on the result.

The tests judge what git does (the conflicted paths) and what verify says about the merged
tree. REF is what CI uses: ``git merge-base <target> <merge commit>``, where the merge commit
has the target's tip as its first parent (docs/guides/ci-cd-maintenance.md), so it is the
target's tip. The fork-point REF is recorded only as a variant. A row that names a resolution
(keep the file, delete it, pick one side) applies it on a checkout of the conflicted tree and
verifies that.

Rows are built with the real commands wherever the row says "main migrates", "maintenance
runs" or "a client ratifies" (``migrate``, ``Store.maintain``, ``Store.ratify``, ``Store.compact``);
hand-built bytes appear only where the row is about content a command would not produce.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§8, §9 T-G1, §28)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sidegraph.migrate import migrate
from sidegraph.schema import AnchorBinding, Decision, Descriptor, Domain, Entity, Fact
from sidegraph.store import Store, _domain_canonical_payload, _entity_identity_payload
from sidegraph.store_format import (
    inventory_from_disk,
    marker_text,
)
from sidegraph.store_layout import (
    StoreRequirementError,
    check_store_requirement,
)
from sidegraph.store_rules import effective_all
from sidegraph.verify import verify_against, verify_snapshot
from tests.store_format2_support import (
    GITIGNORE,
    codes,
    commit_all,
    decision_body,
    domain_body,
    entity_body,
    fact_body,
    git,
    git_env,
    git_repo,  # noqa: F401  (fixture)
    ids_in_bucket,
    make_store,
    put_file,
    validator,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration]

SPEC = Path(__file__).resolve().parents[1] / (
    "design/superpowers/specs/2026-10-20-packed-store-format-design.md"
)
T0 = "2026-01-10T00:00:00Z"  # the base
T1 = "2026-02-10T00:00:00Z"  # main's commits
T2 = "2026-02-20T00:00:00Z"  # the branch's commits
STORE = ".sidegraph"

ROW_TESTS: dict[str, str] = {}  # §8 row label -> the test that covers it


def row(label: str) -> Callable[[Callable], Callable]:
    """Register the test as the one that covers the §8 row ``label`` (``r01`` ... ``r31``,
    ``revert`` for the row §28 adds)."""

    def mark(fn: Callable) -> Callable:
        assert label not in ROW_TESTS, f"two tests for {label}"
        ROW_TESTS[label] = fn.__name__
        return fn

    return mark


# -- the harness --


class Ctx:
    """One working tree on the repository under test, as an op sees it."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.store = repo / STORE
        self.last_migration = ""

    def git(self, *args: str) -> str:
        return git(self.repo, *args)

    def commit(self, msg: str, date: str) -> str:
        return commit_all(self.repo, msg, date=date)

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain").strip())

    def fold(self, date: str, days: int = 0) -> None:
        """Commit what is pending, run maintenance (a real ``Store.maintain``), commit it."""
        if self.dirty():
            self.commit("pending", date)
        with Store(self.store) as s:
            report = s.maintain(settled_days=days, dry_run=False, any_branch=True)
        assert report.stopped is None, report.stopped
        self.commit("maintenance", date)

    def migrate(self, date: str, shards: int = 64) -> None:
        if self.dirty():
            self.commit("pending", date)
        migrate(self.store, shards=shards, dry_run=False, any_branch=True)
        self.last_migration = self.commit("migration", date)

    def compact_format1(self) -> None:
        """Format 1's ``sidegraph-compact`` (``Store.compact``): terminal records to a new
        archive segment, their files removed."""
        with Store(self.store) as s:
            s.compact()

    def require(self, version: str, running: str = "0.99.0") -> None:
        with Store(self.store) as s:
            s.raise_requirement(version, running=running)

    def bind(self, rid: str, *entity_ids: str, relation: str = "affects") -> None:
        """Add anchors with the store's own writer (it rewrites the record's whole list)."""
        with Store(self.store) as s:
            for eid in entity_ids:
                s.add_binding(
                    AnchorBinding(record_id=rid, entity_id=eid, tier=2, relation=relation)  # type: ignore[arg-type]
                )

    def move_entity(self, eid: str, file_path: str) -> None:
        """What sync's ``moved`` outcome does: the real writer persists the entity with a new
        durable descriptor path (a node-only refresh writes no file)."""
        with Store(self.store) as s:
            entity = s.get_entity(eid)
            assert entity is not None and entity.descriptor is not None
            entity.descriptor = Descriptor(name=entity.descriptor.name, file_path=file_path)
            s.upsert_entity(entity)

    def ratify(self, did: str, actor: str) -> None:
        with Store(self.store) as s:
            s.ratify(did, actor=actor)

    def drop(self, did: str) -> None:
        with Store(self.store) as s:
            s.drop(did)

    def ratify_fact(self, fid: str, actor: str) -> None:
        with Store(self.store) as s:
            s.ratify_fact(fid, actor=actor)

    def drop_fact(self, fid: str) -> None:
        with Store(self.store) as s:
            s.drop_fact(fid)

    def read(self, rel: str) -> bytes:
        return (self.store / rel).read_bytes()

    def write(self, rel: str, data: bytes) -> None:
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, rel: str) -> None:
        (self.store / rel).unlink()

    def take(self, side: str, rel: str) -> None:
        """Resolve ``rel`` to one side's version: ``main`` or ``pr`` (the file as committed)."""
        self.write(rel, git_show_bytes(self.repo, f"origin/{side}", f"{STORE}/{rel}"))


Op = Callable[[Ctx], None]


def git_show_bytes(repo: Path, rev: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
    ).stdout


def base_store(*, fmt: int = 2, n: int = 64) -> Op:
    """An empty committed store: marker, ``.gitattributes`` and the index ignore."""

    def op(c: Ctx) -> None:
        make_store(c.repo, fmt=fmt, n=n)
        (c.repo / ".gitattributes").write_text("*.shard -text\n", encoding="utf-8")
        (c.store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    return op


def seq(*ops: Op) -> Op:
    def op(c: Ctx) -> None:
        for o in ops:
            o(c)

    return op


def loose_base(*records: tuple[str, dict], binds: Iterable[tuple[str, tuple[str, ...]]] = ()) -> Op:
    """A format-2 store of per-record files: ``records``, and ``binds`` ``(record id, entity
    ids)`` written by ``Store.add_binding``."""

    def op(c: Ctx) -> None:
        base_store()(c)
        for rec in records:
            put_file(c.store, rec[0], rec[1])
        for rid, entities in binds:
            c.bind(rid, *entities)

    return op


def packed_base(
    *records: tuple[str, dict], binds: Iterable[tuple[str, tuple[str, ...]]] = ()
) -> Op:
    """The same, folded into shards by a real maintenance run."""

    def op(c: Ctx) -> None:
        loose_base(*records, binds=binds)(c)
        c.fold(T0)

    return op


@dataclass
class MergeResult:
    conflicts: set[str]  # store-relative paths git reports conflicted
    tree: str
    kinds: set[str] = field(default_factory=set)  # "modify/delete", "add/add", "content"
    bare: Path = Path()
    main: str = ""
    pr: str = ""
    root: Path = Path()


def _git_bare(bare: Path, *args: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(bare), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
    )
    assert proc.returncode in ok, f"git {args}: {proc.stderr}"
    return proc


def merge_case(
    repo: Path,
    base: Op,
    main_ops: Op | None = None,
    branch_ops: Op | None = None,
    *,
    lands: str = "merge",
) -> MergeResult:
    """Build BASE on ``main``, cut ``pr`` from it, apply ``branch_ops`` there and ``main_ops``
    (each may commit; what is left over is committed), clone the repository bare and run
    ``git merge-tree --write-tree --name-only main pr`` there. As §8 states, main's side lands
    by a merge commit (``lands="merge"``: the ops run on a feature branch that is merged with
    ``--no-ff``); ``lands="direct"`` commits them on ``main`` itself (the fast-forward shape)."""
    c = Ctx(repo)
    base(c)
    c.commit("base", T0)
    c.git("branch", "pr")
    c.git("checkout", "-q", "pr")
    if branch_ops is not None:
        branch_ops(c)
    if c.dirty():
        c.commit("branch", T2)
    c.git("checkout", "-q", "main")
    if main_ops is not None:
        if lands == "merge":
            c.git("checkout", "-q", "-b", "feat")
        main_ops(c)
        if c.dirty():
            c.commit("main", T1)
        if lands == "merge":
            c.git("checkout", "-q", "main")
            if c.git("rev-list", "--count", "main..feat").strip() != "0":
                _merge_no_ff(c, "feat", T1)
    return merge_of(repo)


def _merge_no_ff(c: Ctx, branch: str, date: str) -> None:
    env = {**git_env(), "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    subprocess.run(
        ["git", "-C", str(c.repo), "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def new_repo(parent: Path, name: str) -> Path:
    """A second repository beside ``git_repo`` for a test that merges twice."""
    repo = parent / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "gc.auto", "0")
    return repo


def merge_of(repo: Path) -> MergeResult:
    bare = repo.parent / f"bare{len(list(repo.parent.glob('bare*')))}.git"
    cloned = subprocess.run(
        ["git", "clone", "-q", "--no-local", "--bare", str(repo), str(bare)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
    )
    assert cloned.returncode == 0, cloned.stderr
    proc = _git_bare(bare, "merge-tree", "--write-tree", "--name-only", "main", "pr", ok=(0, 1))
    lines = proc.stdout.split("\n")
    conflicts: set[str] = set()
    for name in lines[1:]:
        if not name:
            break
        assert name.startswith(STORE + "/"), name
        conflicts.add(name[len(STORE) + 1 :])
    kinds = set(re.findall(r"CONFLICT \(([^)]+)\)", proc.stdout))
    assert (proc.returncode == 1) == bool(conflicts), proc.stdout
    return MergeResult(
        conflicts=conflicts,
        tree=lines[0],
        kinds=kinds,
        bare=bare,
        main=_git_bare(bare, "rev-parse", "main").stdout.strip(),
        pr=_git_bare(bare, "rev-parse", "pr").stdout.strip(),
        root=repo.parent,
    )


def merge_commit(res: MergeResult) -> str:
    """The merge commit CI checks out: the target's tip first, the PR second."""
    sha = _git_bare(
        res.bare, "commit-tree", res.tree, "-p", res.main, "-p", res.pr, "-m", "merge"
    ).stdout.strip()
    _git_bare(res.bare, "update-ref", "refs/heads/merge", sha)
    return sha


def ci_ref(bare: Path, target: str, head: str) -> str:
    """What CI computes: ``git merge-base <target> <head>``, ``head`` being the merge commit
    (the target's tip is its first parent, so the answer is the target's tip)."""
    return _git_bare(bare, "merge-base", target, head).stdout.strip()


@dataclass
class Verdict:
    snapshot: set[str]
    against: set[str]
    ref: str
    ctx: Ctx
    messages: list[str] = field(default_factory=list)

    @property
    def all(self) -> set[str]:
        return self.snapshot | self.against


_checkouts = 0


def checkout(res: MergeResult) -> tuple[Ctx, str]:
    """A work tree on the merge commit (conflict markers and all) and CI's REF."""
    global _checkouts
    _checkouts += 1
    head = merge_commit(res)
    ref = ci_ref(res.bare, "main", head)
    assert ref == res.main, "the merge commit's first parent is the target's tip"
    path = res.root / f"checkout{_checkouts}"
    subprocess.run(
        ["git", "clone", "-q", "--no-local", str(res.bare), str(path)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
    )
    git(path, "checkout", "-q", "merge")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "gc.auto", "0")
    return Ctx(path), ref


def verdict(
    res: MergeResult,
    resolve: Op | None = None,
    *,
    ref: str | None = None,
    then: Op | None = None,
) -> Verdict:
    """Verify the merged tree: resolve the conflicts with ``resolve`` (uncommitted), optionally
    commit and run ``then`` (maintenance on the resolved tree), and run snapshot and
    ``--against`` CI's REF (or ``ref``) on the working tree."""
    c, ci = checkout(res)
    if resolve is not None:
        resolve(c)
    if then is not None:
        c.commit("resolution", T2)
        then(c)
    use = ref if ref is not None else ci
    snap = verify_snapshot(c.store)
    ag = verify_against(c.store, use)
    return Verdict(
        codes(snap), codes(ag), use, c, [f"{v.code} {v.path}: {v.detail}" for v in snap + ag]
    )


def rel(*parts: str) -> str:
    return "/".join(parts)


def effective_raw(store: Path) -> dict[tuple[str, str], object]:
    inv = inventory_from_disk(store, validate=validator)
    return {k: e.raw for k, e in effective_all(inv).items()}


def exact(v: Verdict, snapshot: Iterable[str] = (), against: Iterable[str] = ()) -> None:
    """The verify column, exactly: the snapshot's code set and ``--against``'s."""
    assert (v.snapshot, v.against) == (set(snapshot), set(against)), v.messages


def served_bindings(store: Path, rid: str) -> set[str]:
    """The entities the store SERVES for ``rid``: the effective record, and what a reopened
    ``Store`` reads back through its index."""
    eff = effective_raw(store)[("bindings", rid)]
    from_files = {b["entity_id"] for b in eff}  # type: ignore[union-attr]
    with Store(store) as s:
        from_index = {b.entity_id for b in s.bindings_for_record(rid)}
    assert from_files == from_index, (from_files, from_index)
    return from_files


def served_status(store: Path, did: str) -> str:
    return str(effective_raw(store)[("decision", did)]["status"])  # type: ignore[index]


def canon(t: str, body: dict) -> dict:
    """``body`` as the store's own writer would emit it (model defaults filled), so a later
    ``Store`` edit changes only the keys the edit means to change."""
    if t == "decision":
        return Decision.model_validate(body).model_dump(mode="json")
    if t == "fact":
        return Fact.model_validate(body).model_dump(mode="json")
    if t == "entity":
        return _entity_identity_payload(Entity.model_validate(body))
    return _domain_canonical_payload(Domain.model_validate(body))


ENTITY_AT = {"name": "E", "file_path": "src/e.py"}


def D(rid: str, **over: object) -> tuple[str, dict]:
    return ("decision", canon("decision", decision_body(rid, **over)))


def F(rid: str, **over: object) -> tuple[str, dict]:
    return ("fact", canon("fact", fact_body(rid, **over)))


def E(rid: str, **over: object) -> tuple[str, dict]:
    return ("entity", canon("entity", entity_body(rid, **over)))


def DM(rid: str, **over: object) -> tuple[str, dict]:
    return ("domain", canon("domain", domain_body(rid, **over)))


def put(rec: tuple[str, dict]) -> Op:
    """Write one canonical record as its per-record file."""
    return lambda c: put_file(c.store, rec[0], rec[1]) and None


# -- §8 rows 1-4: plain edits --


@row("r01")
def test_row01_both_branches_add_records(git_repo):
    """§8: both branches add records — git clean, verify clean."""
    res = merge_case(
        git_repo,
        packed_base(D("D1")),
        put(D("DM1")),
        put(D("DB1")),
    )
    assert res.conflicts == set()
    v = verdict(res)
    assert v.all == set(), v.messages


@row("r02")
def test_row02_edits_of_different_packed_records(git_repo):
    """§8: both branches edit different packed records — clean (two new per-record files)."""
    res = merge_case(
        git_repo,
        packed_base(D("D1"), D("D2")),
        lambda c: c.ratify("D1", "main"),
        lambda c: c.ratify("D2", "branch"),
    )
    assert res.conflicts == set()
    v = verdict(res)
    assert v.all == set(), v.messages
    assert {p.name for p in (v.ctx.store / "decisions").glob("*.json")} == {"D1.json", "D2.json"}


@row("r03")
def test_row03_same_packed_record_edited_twice(git_repo):
    """§8: both branches edit the same packed record — add/add conflict on its file."""
    res = merge_case(
        git_repo,
        packed_base(D("D1")),
        lambda c: c.ratify("D1", "main"),
        lambda c: c.ratify("D1", "branch"),
    )
    assert res.conflicts == {"decisions/D1.json"}
    assert res.kinds == {"add/add"}
    # as today: resolving to the target's own file leaves nothing to judge
    v = verdict(res, lambda c: c.take("main", "decisions/D1.json"))
    assert v.all == set(), v.messages


@row("r04")
def test_row04_ratify_against_drop_of_a_packed_record(git_repo):
    """§8: ratify A vs drop A, A packed — add/add conflict on ``decisions/A.json``."""
    res = merge_case(
        git_repo,
        packed_base(D("A")),
        lambda c: c.ratify("A", "main"),
        lambda c: c.drop("A"),
    )
    assert res.conflicts == {"decisions/A.json"}
    assert res.kinds == {"add/add"}
    kept_main = verdict(res, lambda c: c.take("main", "decisions/A.json"))
    assert kept_main.all == set(), kept_main.messages
    # the other side's values judged against CI's REF (main's accepted file): a dropped
    # decision cannot follow an accepted one
    kept_pr = verdict(res, lambda c: c.take("pr", "decisions/A.json"))
    exact(kept_pr, against={"illegal-status-jump", "illegal-field-change"})


# -- §8 rows 5-13: folds under an open edit --


@row("r14")
def test_row14_both_branches_add_different_anchors(git_repo):
    """§8: both branches add different anchors to packed A — add/add conflict on
    ``bindings/A.json``; guide recipe: keep the union."""
    res = merge_case(
        git_repo,
        packed_base(D("A"), E("E1"), E("E2"), E("E3"), binds=[("A", ("E1",))]),
        lambda c: c.bind("A", "E2"),
        lambda c: c.bind("A", "E3"),
    )
    assert res.conflicts == {"bindings/A.json"}
    assert res.kinds == {"add/add"}
    union = verdict(res, lambda c: (c.take("main", "bindings/A.json"), c.bind("A", "E3")))
    exact(union)
    assert served_bindings(union.ctx.store, "A") == {"E1", "E2", "E3"}


@row("r16")
def test_row16_sync_moved_on_two_branches_different_targets(git_repo):
    """§8: sync ``moved`` of packed entity E on two branches, different targets — add/add
    conflict on ``entities/E.json``; verify as today."""
    res = merge_case(
        git_repo,
        packed_base(E("E", descriptor=ENTITY_AT)),
        lambda c: c.move_entity("E", "src/main.py"),
        lambda c: c.move_entity("E", "src/branch.py"),
    )
    assert res.conflicts == {"entities/E.json"}
    assert res.kinds == {"add/add"}
    v = verdict(res, lambda c: c.take("pr", "entities/E.json"))
    exact(v)
    assert effective_raw(v.ctx.store)[("entity", "E")]["descriptor"]["file_path"] == "src/branch.py"  # type: ignore[index]


# -- §8 rows 18-25: migration --


def format1_base(*extra: Op) -> Op:
    """A committed format-1 store (marker 1.0.0 plus a requirement) of canonical records D1,
    D2, F1, E1, DM1 and the bindings of D2, plus ``extra``."""

    def op(c: Ctx) -> None:
        make_store(c.repo, fmt=1)
        (c.store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        for rec in (D("D1"), D("D2"), F("F1"), E("E1"), DM("DM1")):
            put_file(c.store, rec[0], rec[1])
        c.bind("D2", "E1")
        for e in extra:
            e(c)

    return op


@row("r18")
def test_row18_migration_on_main_branch_adds_records(git_repo):
    """§8: migration on main; a branch adds records — clean, verify clean."""
    res = merge_case(
        git_repo,
        format1_base(),
        lambda c: c.migrate(T1),
        put(D("DNEW")),
    )
    assert res.conflicts == set()
    v = verdict(res)
    assert v.all == set(), v.messages


@row("r19")
def test_row19_migration_on_main_branch_edits_an_existing_record(git_repo):
    """§8: migration on main; a branch edits an existing record — modify/delete; the keep-file
    recipe, as in row 5."""
    res = merge_case(
        git_repo,
        format1_base(),
        lambda c: c.migrate(T1),
        lambda c: c.ratify("D1", "branch"),
    )
    assert res.conflicts == {"decisions/D1.json"}
    assert res.kinds == {"modify/delete"}
    kept = verdict(res, lambda c: c.take("pr", "decisions/D1.json"))
    assert kept.all == set(), kept.messages


def _two_markers_case(git_repo, branch_op: Op) -> MergeResult:
    return merge_case(git_repo, format1_base(), lambda c: c.migrate(T1), branch_op)


@row("r20")
@row("r23")
def test_row20_pre_migration_branch_raised_the_requirement(git_repo):
    """§8: migration on main; a pre-migration branch raised the requirement
    (``sidegraph-require``) — conflict on ``format``; resolve to the major-2 marker with the
    higher requirement; a major-1 resolution with shards present is refused by 0.17 and
    completed by ``sidegraph-migrate``."""
    res = _two_markers_case(git_repo, lambda c: c.require("0.16.9", running="0.17.0"))
    assert res.conflicts == {"format"}
    resolved = verdict(
        res, lambda c: c.write("format", marker_text(2, "0.17.0", 64).encode("utf-8"))
    )
    exact(resolved)
    # a major-1 resolution keeps the shards: refused by 0.17, completed by migrate
    major1 = verdict(res, lambda c: c.take("pr", "format"))
    exact(major1, {"shards-without-format2"}, {"format-downgrade"})
    c = major1.ctx
    with pytest.raises(StoreRequirementError, match="sidegraph-migrate"):
        check_store_requirement(c.store)
    c.commit("resolved to major 1", T2)
    migrate(c.store, dry_run=False, any_branch=True)
    assert marker_text(2, "0.17.0", 64) == c.read("format").decode("utf-8")
    c.commit("completed", T2)
    check_store_requirement(c.store)  # the store is readable again
    assert verify_snapshot(c.store) == []
    assert verify_against(c.store, major1.ref) == []


@row("r27")
def test_row27_domain_snapshot_on_two_branches(git_repo):
    """§8: ``domain-snapshot.json`` on two branches — conflict (unchanged from today)."""

    def snapshot(tag: str) -> Op:
        return lambda c: c.write("domain-snapshot.json", json.dumps({"tag": tag}).encode() + b"\n")

    res = merge_case(git_repo, packed_base(D("D0")), snapshot("main"), snapshot("branch"))
    assert res.conflicts == {"domain-snapshot.json"}
    assert res.kinds == {"add/add"}


# -- §8 rows 28-30: format-1 compaction in a pre-migration branch --


# -- §8 row 31: maintenance on two branches --


def _newest_in_bucket() -> tuple[list[str], str, str]:
    """Two decision ids that sort after the two packed ones in the same bucket."""
    ids = ids_in_bucket(7, 4, prefix="N")
    return ids[:2], ids[2], ids[3]


@row("r31")
def test_row31_maintenance_on_two_branches_when_separated(git_repo):
    """§8: maintenance on two branches — clean when the inserted lines are separated."""
    in_a = ids_in_bucket(3, 1, prefix="P")[0]
    in_b = ids_in_bucket(40, 1, prefix="P")[0]
    res = merge_case(
        git_repo,
        packed_base(D("D0")),
        lambda c: (put(D(in_a))(c), c.fold(T1)),
        lambda c: (put(D(in_b))(c), c.fold(T2)),
    )
    assert res.conflicts == set()
    v = verdict(res)
    # the PR side folded a record it created: shard-body-without-source (Opus rev 13 finding 2)
    exact(v, (), {"shard-body-without-source"})


# -- §28 Opus 2: GitHub's Revert of a packed entity or bindings edit --


# -- the readers' major gate and the matrix's own bookkeeping --
