import sqlite3
import subprocess
from datetime import UTC, datetime

from sidegraph.cli import sync_main
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Domain,
    Entity,
    Provenance,
)
from sidegraph.store import Store

GRAPH_B = {
    "built_at_commit": "vB",
    "nodes": [
        # stable: same id, same file  -> unchanged
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        # old_fn renamed away         -> orphaned
        {
            "id": "r2",
            "label": "new_fn()",
            "norm_label": "new_fn()",
            "file_type": "code",
            "source_file": "b.py",
            "community": 1,
        },
        # mover: moved c.py -> d.py, new id -> moved
        {
            "id": "m2",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "d.py",
            "community": 2,
        },
        # dup_fn now exists twice     -> ambiguous
        {
            "id": "d2",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "e.py",
            "community": 2,
        },
        {
            "id": "d3",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "f.py",
            "community": 3,
        },
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    import json

    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_cli_sync_reports_and_stamps(tmp_path, capsys):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "t.db"
    s = Store(db)
    s.upsert_entity(
        Entity(
            canonical_name="old_fn",
            descriptor=Descriptor(name="old_fn", file_path="b.py"),
            last_seen_node_id="r1",
        )
    )
    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "orphaned" in out and "old_fn" in out  # escalation surface
    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    assert "up to date" in capsys.readouterr().out  # second run gated


def test_cli_sync_force_reruns(tmp_path, capsys):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "t.db"
    Store(db)
    sync_main(["--db", str(db), "--graph", str(graph)])
    capsys.readouterr()
    assert sync_main(["--db", str(db), "--graph", str(graph), "--force"]) == 0
    assert "up to date" not in capsys.readouterr().out


def test_cli_sync_unreadable_graph_reports_and_fails(tmp_path, capsys):
    assert sync_main(["--db", str(tmp_path / "t.db"), "--graph", str(tmp_path / "no.json")]) == 1
    assert "not readable" in capsys.readouterr().out


def test_cli_sync_store_open_failure_exits_nonzero(tmp_path, capsys):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "bad.db"
    Store(db)  # create a valid store first
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db (git-native store) — corrupt THAT file, not `db` itself.
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 1
    assert "not readable" in capsys.readouterr().out


def test_cli_sync_per_entity_errors_do_not_fail_run(tmp_path, capsys, monkeypatch):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "t.db"
    s = Store(db)
    s.upsert_entity(
        Entity(
            canonical_name="old_fn",
            descriptor=Descriptor(name="old_fn", file_path="b.py"),
            last_seen_node_id="r1",
        )
    )

    real_resolve = GraphifyReader.resolve

    def flaky(self, desc):
        if desc.name == "old_fn":
            raise RuntimeError("boom")
        return real_resolve(self, desc)

    monkeypatch.setattr(GraphifyReader, "resolve", flaky)
    # a per-entity rebind error is a report finding, not a process failure
    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    assert "error: old_fn" in capsys.readouterr().out


def test_cli_sync_reports_repointed_community_bindings(tmp_path, capsys):
    # The printed count sums per-anchor re-points (bindings), not distinct decisions -- the
    # wording must say "binding(s)", not "decision(s)".
    graph_a = _write_graph(
        tmp_path,
        "a.json",
        {
            "built_at_commit": "vA",
            "nodes": [
                {
                    "id": "n1",
                    "label": "fee_gate()",
                    "norm_label": "fee_gate()",
                    "file_type": "code",
                    "source_file": "risk/gate.py",
                    "community": 1,
                }
            ],
            "links": [],
        },
    )
    graph_b = _write_graph(
        tmp_path,
        "b.json",
        {
            "built_at_commit": "vB",
            "nodes": [
                {
                    "id": "n1",
                    "label": "fee_gate()",
                    "norm_label": "fee_gate()",
                    "file_type": "code",
                    "source_file": "risk/gate.py",
                    "community": 7,
                }
            ],
            "links": [],
        },
    )
    db = tmp_path / "t.db"
    s = Store(db)
    e = s.upsert_entity(
        Entity(
            canonical_name="fee_gate",
            descriptor=Descriptor(name="fee_gate", file_path="risk/gate.py"),
        )
    )
    d = s.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))

    assert sync_main(["--db", str(db), "--graph", str(graph_a)]) == 0
    capsys.readouterr()  # establish the community:1 baseline

    assert sync_main(["--db", str(db), "--graph", str(graph_b)]) == 0
    out = capsys.readouterr().out
    assert "re-pointed 1 community binding(s)" in out
    assert "decision(s)" not in out


def test_cli_sync_surfaces_moved_with_old_and_new_file(tmp_path, capsys):
    # The moved rung fails closed without a resolvable repo_root (sync.py's
    # _resolve_repo_root) -- git-init tmp_path so it can confirm c.py is genuinely gone,
    # same as the live checkout the fix targets.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "t.db"
    s = Store(db)
    s.upsert_entity(
        Entity(
            canonical_name="mover_fn",
            descriptor=Descriptor(name="mover_fn", file_path="c.py"),
            last_seen_node_id="m1",
        )
    )
    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "moved: mover_fn" in out
    assert "c.py -> d.py" in out


def test_cli_sync_reports_domains_refreshed(tmp_path, capsys):
    # Padded with 9 unrelated filler communities (10 total) so a single-community
    # path-prefix match sits at 10% of the graph, comfortably under the refresh claim
    # cap's 20% threshold (sync._DOMAIN_CLAIM_CAP) -- see test_sync_domains.py's
    # _FILLER_COMMUNITIES for the same rationale.
    filler = [
        {
            "id": f"filler-{i}",
            "label": f"Filler{i}",
            "norm_label": f"filler{i}",
            "file_type": "code",
            "source_file": f"unrelated/f{i}.py",
            "community": f"filler-c{i}",
        }
        for i in range(9)
    ]
    graph_a = _write_graph(
        tmp_path,
        "a.json",
        {
            "built_at_commit": "vA",
            "nodes": [
                {
                    "id": "n1",
                    "label": "Alpha",
                    "norm_label": "alpha",
                    "file_type": "code",
                    "source_file": "payments/a.py",
                    "community": 1,
                }
            ]
            + filler,
            "links": [],
        },
    )
    graph_b = _write_graph(
        tmp_path,
        "b.json",
        {
            "built_at_commit": "vB",
            "nodes": [
                {
                    "id": "n1",
                    "label": "Alpha",
                    "norm_label": "alpha",
                    "file_type": "code",
                    "source_file": "payments/a.py",
                    "community": 7,
                }
            ]
            + filler,
            "links": [],
        },
    )
    db = tmp_path / "t.db"
    s = Store(db)
    d = s.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            communities=["1"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        )
    )
    s.ratify_domains(accept=[d.domain_id])

    assert sync_main(["--db", str(db), "--graph", str(graph_a)]) == 0
    capsys.readouterr()  # establish the community:1 baseline (no-op refresh)

    assert sync_main(["--db", str(db), "--graph", str(graph_b)]) == 0
    out = capsys.readouterr().out
    assert "refreshed community mapping for 1 domain(s)" in out


def test_cli_sync_warns_on_empty_domains(tmp_path, capsys):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "t.db"
    s = Store(db)
    d = s.add_domain(
        Domain(
            slug="ghost",
            title="Ghost",
            summary="s",
            communities=["999"],
            provenance=Provenance(source="manual"),  # no path_prefixes; "999" not in graph
        )
    )
    s.ratify_domains(accept=[d.domain_id])

    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "possibly empty domains — re-scope or supersede:" in out
    assert "ghost" in out
    assert s.get_domain(d.domain_id).status.value == "accepted"  # status never auto-changed


def test_cli_sync_warns_on_overbroad_domain(tmp_path, capsys):
    """Gate-5 fix 2: a domain whose path_prefixes now resolve to > 20% of all current
    communities is never silently written — sidegraph-sync prints a dedicated warning
    block and keeps the previous mapping."""
    graph = _write_graph(
        tmp_path,
        "b.json",
        {
            "built_at_commit": "v1",
            "nodes": [
                {
                    "id": f"c{c}src",
                    "label": f"Thing{c}",
                    "norm_label": f"thing{c}",
                    "file_type": "code",
                    "source_file": f"domain{c}/f.py",
                    "community": c,
                }
                for c in range(1, 7)
            ]
            + [
                {
                    "id": f"c{c}test",
                    "label": f"test_thing{c}",
                    "norm_label": f"test_thing{c}",
                    "file_type": "code",
                    "source_file": f"tests/test_thing{c}.py",
                    "community": c,
                }
                for c in (1, 2, 3, 4)
            ],
            "links": [],
        },
    )
    db = tmp_path / "t.db"
    s = Store(db)
    d = s.add_domain(
        Domain(
            slug="test-infra",
            title="Test Infra",
            summary="s",
            communities=["1"],
            path_prefixes=["tests"],
            provenance=Provenance(source="manual"),
        )
    )
    s.ratify_domains(accept=[d.domain_id])

    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "path rule too broad — path contribution dropped" in out
    assert "test-infra" in out
    assert "4/6 communities" in out
    assert s.get_domain(d.domain_id).communities == ["1"]  # unchanged, never zeroed


def test_cli_sync_warns_on_domain_slug_conflict(tmp_path, capsys):
    """design §6: a cross-branch merge landing two branches' independently accepted
    domains with the same slug — different domain_ids, so the files merge in cleanly with
    no git conflict, and nothing at write time catches it. sidegraph-sync's report line
    is what surfaces it to a bare CLI user."""
    import json

    from sidegraph.schema import DomainStatus

    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    db = tmp_path / "t.db"
    Store(db).close()  # bootstrap the layout, nothing in it yet

    def _write_domain_file(domain_id, title):
        d = Domain(
            domain_id=domain_id,
            slug="payments",
            title=title,
            summary="s",
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
        data = d.model_dump(mode="json")
        data.pop("communities", None)
        path = db / "domains" / f"{domain_id}.json"
        path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")

    _write_domain_file("01SLUGCLIBRANCHA0000001", "Payments (branch A)")
    _write_domain_file("01SLUGCLIBRANCHB0000002", "Payments (branch B)")

    assert sync_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "slug conflicts — drop one (sidegraph-ratify --drop <loser-id>):" in out
    assert (
        "slug conflict: 'payments' held by 2 live domains "
        "(01SLUGCLIBRANCHA0000001, 01SLUGCLIBRANCHB0000002) — drop one" in out
    )


# -- --db resolution (bare run, no --db / no env): see config.resolve_store_path. Full
# precedence-matrix coverage (explicit > SIDEGRAPH_DIR > SIDEGRAPH_DB (deprecated,
# dispatched) > existing .sidegraph/ > default .sidegraph) lives in tests/test_config.py;
# these are the CLI-level integration checks that sync_main actually wires through it.


def _seed_old_fn(db_path):
    s = Store(db_path)
    s.upsert_entity(
        Entity(
            canonical_name="old_fn",
            descriptor=Descriptor(name="old_fn", file_path="b.py"),
            last_seen_node_id="r1",
        )
    )


def _reset_deprecation_warning(monkeypatch):
    import sidegraph.config as config

    monkeypatch.setattr(config, "_deprecation_warned", False)


def test_cli_sync_bare_prefers_existing_dot_sidegraph_store(tmp_path, capsys, monkeypatch):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    dot_dir = tmp_path / ".sidegraph"
    _seed_old_fn(dot_dir)  # ".sidegraph" IS the store now, not a nested "decisions.db"

    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sync_main(["--graph", str(graph)]) == 0
    captured = capsys.readouterr()
    assert "orphaned" in captured.out and "old_fn" in captured.out
    assert captured.err == ""  # store already existed -> no warning


def test_cli_sync_bare_creates_default_dot_sidegraph_with_warning(tmp_path, capsys, monkeypatch):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sync_main(["--graph", str(graph)]) == 0
    captured = capsys.readouterr()
    assert "no tracked entities" in captured.out  # fresh, empty store
    assert (tmp_path / ".sidegraph" / "format").is_file()
    assert "warning: creating new store at .sidegraph" in captured.err
    assert "--db or set SIDEGRAPH_DIR" in captured.err


def test_cli_sync_sidegraph_dir_wins_over_dot_sidegraph_and_sidegraph_db(
    tmp_path, capsys, monkeypatch
):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    dot_dir = tmp_path / ".sidegraph"
    Store(dot_dir)  # exists but must be ignored -- SIDEGRAPH_DIR wins

    via_db_env = tmp_path / "via-db-env.db"
    Store(via_db_env)  # exists but must be ignored -- SIDEGRAPH_DIR wins over SIDEGRAPH_DB too

    dir_env = tmp_path / "via-dir-env"
    _seed_old_fn(dir_env)

    monkeypatch.setenv("SIDEGRAPH_DIR", str(dir_env))
    monkeypatch.setenv("SIDEGRAPH_DB", str(via_db_env))
    monkeypatch.chdir(tmp_path)
    assert sync_main(["--graph", str(graph)]) == 0
    captured = capsys.readouterr()
    assert "orphaned" in captured.out and "old_fn" in captured.out
    assert captured.err == ""  # SIDEGRAPH_DIR wins outright -- no deprecation note either


def test_cli_sync_sidegraph_db_back_compat_wins_over_dot_sidegraph(tmp_path, capsys, monkeypatch):
    """SIDEGRAPH_DB is deprecated but still honored (design §5) -- and still takes
    precedence over an existing ``.sidegraph/`` when SIDEGRAPH_DIR is unset."""
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    _reset_deprecation_warning(monkeypatch)
    dot_dir = tmp_path / ".sidegraph"
    Store(dot_dir)  # exists but must be ignored -- SIDEGRAPH_DB wins

    custom_db = tmp_path / "custom.db"
    _seed_old_fn(custom_db)

    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(custom_db))
    monkeypatch.chdir(tmp_path)
    assert sync_main(["--graph", str(graph)]) == 0
    captured = capsys.readouterr()
    assert "orphaned" in captured.out and "old_fn" in captured.out
    assert "SIDEGRAPH_DB is deprecated" in captured.err
    assert "creating new store" not in captured.err  # store already existed -> no such warning


def test_cli_sync_explicit_db_wins_over_everything(tmp_path, capsys, monkeypatch):
    graph = _write_graph(tmp_path, "b.json", GRAPH_B)
    dot_dir = tmp_path / ".sidegraph"
    Store(dot_dir)
    Store(tmp_path / "dir-env")
    Store(tmp_path / "env.db")

    explicit_db = tmp_path / "explicit.db"
    _seed_old_fn(explicit_db)

    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "dir-env"))
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "env.db"))
    monkeypatch.chdir(tmp_path)
    assert sync_main(["--db", str(explicit_db), "--graph", str(graph)]) == 0
    captured = capsys.readouterr()
    assert "orphaned" in captured.out and "old_fn" in captured.out
    assert captured.err == ""
