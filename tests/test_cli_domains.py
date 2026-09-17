"""``sidegraph-domains`` CLI — bootstrap + add subcommands (mind-model layer, M3)."""

import json
import sqlite3

import pytest

from sidegraph.cli import domains_main, ratify_main
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.store import Store

GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": f"n{i}",
            "label": f"Thing{i}",
            "norm_label": f"thing{i}",
            "file_type": "code",
            "source_file": f"domainA/f{i}.py",
            "community": 10,
        }
        for i in range(6)
    ]
    + [
        {
            "id": f"m{i}",
            "label": f"Other{i}",
            "norm_label": f"other{i}",
            "file_type": "code",
            "source_file": f"domainB/f{i}.py",
            "community": 20,
        }
        for i in range(5)
    ],
    "links": [],
}

LABELS = {"10": "Alpha Domain"}


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _setup_graph(tmp_path, labels=None):
    graph = _write(tmp_path, "g.json", GRAPH)
    if labels is not None:
        _write(tmp_path, ".graphify_labels.json", labels)
    return graph


# -- bootstrap ------------------------------------------------------------------------


def test_cli_domains_bootstrap_reports_and_writes(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "proposed 2 domain(s) (skipped: 0 existing)" in out

    store = Store(db)
    domains = list(store.iter_domains())
    assert len(domains) == 2
    assert all(d.status == DomainStatus.PROPOSED for d in domains)


def test_cli_domains_bootstrap_dry_run_writes_nothing(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would propose 2 domain(s)" in out
    assert "alpha-domain" in out

    store = Store(db)
    assert list(store.iter_domains()) == []


def test_cli_domains_bootstrap_min_members_flag(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert (
        domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--min-members", "6"])
        == 0
    )
    store = Store(db)
    domains = list(store.iter_domains())
    assert len(domains) == 1
    assert domains[0].slug == "alpha-domain"


def test_cli_domains_bootstrap_paths_flag(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert (
        domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--paths", "domainB"])
        == 0
    )
    store = Store(db)
    domains = list(store.iter_domains())
    assert len(domains) == 1
    assert domains[0].communities == ["20"]


def test_cli_domains_bootstrap_limit_flag(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--limit", "1"]) == 0
    store = Store(db)
    domains = list(store.iter_domains())
    assert len(domains) == 1
    assert domains[0].slug == "alpha-domain"


def test_cli_domains_bootstrap_idempotent_rerun(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)])
    capsys.readouterr()
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "proposed 0 domain(s) (skipped: 2 existing)" in out


def test_cli_domains_bootstrap_negative_limit_rejected(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--limit", "-1"]) == 1
    out = capsys.readouterr().out
    assert "--limit" in out
    assert not db.exists()


def test_cli_domains_bootstrap_zero_min_members_rejected(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert (
        domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--min-members", "0"])
        == 1
    )
    out = capsys.readouterr().out
    assert "--min-members" in out
    assert not db.exists()


def test_cli_domains_bootstrap_unreadable_graph_exits_one(tmp_path, capsys):
    assert (
        domains_main(
            ["bootstrap", "--db", str(tmp_path / "t.db"), "--graph", str(tmp_path / "no.json")]
        )
        == 1
    )
    assert "not readable" in capsys.readouterr().out


def test_cli_domains_bootstrap_store_open_failure_exits_one(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "bad.db"
    Store(db)
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db (git-native store — see docs/reference/store-format.md); corrupt THAT
    # file, not `db` itself.
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 1
    assert "not readable" in capsys.readouterr().out


def test_cli_domains_bootstrap_empty_graph_exits_zero(tmp_path, capsys):
    empty_graph = {"nodes": [], "links": []}
    graph = _write(tmp_path, "g.json", empty_graph)
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "proposed 0 domain(s) (skipped: 0 existing)" in out


def _many_communities_graph(n):
    """``n`` distinct significant (5-member) communities, no labels, no links — enough to
    trip the "large run" stderr hint without needing real community structure."""
    nodes = [
        {
            "id": f"c{c}n{i}",
            "label": f"Thing{c}_{i}",
            "norm_label": f"thing{c}_{i}",
            "file_type": "code",
            "source_file": f"domain{c}/f{i}.py",
            "community": c,
        }
        for c in range(n)
        for i in range(5)
    ]
    return {"built_at_commit": "v1", "nodes": nodes, "links": []}


def test_cli_domains_bootstrap_large_run_prints_stderr_hint(tmp_path, capsys):
    graph = _write(tmp_path, "g.json", _many_communities_graph(51))
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    out, err = capsys.readouterr()
    assert "proposed 51 domain(s)" in out
    assert "note: about to propose 51 domains" in err
    assert "--min-members/--limit" in err


def test_cli_domains_bootstrap_small_run_has_no_stderr_hint(tmp_path, capsys):
    graph = _setup_graph(tmp_path, labels=LABELS)
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    assert capsys.readouterr().err == ""


def test_cli_domains_bootstrap_dry_run_large_has_no_stderr_hint(tmp_path, capsys):
    """The hint is about a large NON-dry-run write; a dry run commits nothing, so it
    shouldn't nag about ratifying selectively."""
    graph = _write(tmp_path, "g.json", _many_communities_graph(51))
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--dry-run"]) == 0
    assert capsys.readouterr().err == ""


# -- default --limit + before-write warning (BUG B/C: scale-aware default) ------------


def test_cli_domains_bootstrap_default_limit_caps_write(tmp_path, capsys):
    """BUG B mirrored onto the CLI: with no --limit given, a monorepo-scale candidate set
    (150 significant communities) writes only the default 100, never the full flood."""
    graph = _write(tmp_path, "g.json", _many_communities_graph(150))
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)]) == 0
    out, err = capsys.readouterr()

    store = Store(db)
    assert len(list(store.iter_domains())) == 100
    assert "proposed 100 domain(s)" in out
    assert "150 significant communities found" in err
    assert "showing only the top 100" in err


def test_cli_domains_bootstrap_limit_zero_is_unlimited(tmp_path, capsys):
    """The "all" convention: --limit 0 writes the FULL candidate set, unflagged."""
    graph = _write(tmp_path, "g.json", _many_communities_graph(150))
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--limit", "0"]) == 0
    out, err = capsys.readouterr()

    store = Store(db)
    assert len(list(store.iter_domains())) == 150
    assert "proposed 150 domain(s)" in out
    assert "significant communities found" not in err


def test_cli_domains_bootstrap_explicit_limit_still_overrides_default(tmp_path, capsys):
    graph = _write(tmp_path, "g.json", _many_communities_graph(150))
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--limit", "40"]) == 0
    store = Store(db)
    assert len(list(store.iter_domains())) == 40


def test_cli_domains_bootstrap_dry_run_default_limit_notes_the_cap(tmp_path, capsys):
    """Dry run shares the same default --limit (consistency with the tool), but must NOTE
    the cap rather than silently showing a partial "full picture" (team decision: dry run
    stays capped by default too, just clearly flagged) -- and must NOT print the >50
    "ratify selectively" nag, which is only about a real write."""
    graph = _write(tmp_path, "g.json", _many_communities_graph(150))
    db = tmp_path / "t.db"
    assert domains_main(["bootstrap", "--db", str(db), "--graph", str(graph), "--dry-run"]) == 0
    out, err = capsys.readouterr()

    assert "would propose 100 domain(s)" in out
    assert "150 significant communities found" in err
    assert "showing only the top 100" in err
    assert "ratify selectively" not in err
    assert list(Store(db).iter_domains()) == []  # dry-run: nothing written


def test_cli_domains_bootstrap_warns_before_write_even_if_write_crashes(
    tmp_path, capsys, monkeypatch
):
    """BUG C: the over-threshold/truncation nags must come from a READ-ONLY pre-check,
    printed BEFORE `bootstrap_domains` starts writing -- proven by making the write itself
    blow up on the very first record and confirming the nag still reached stderr. Under the
    old (buggy) ordering the nag was computed from the finished report AFTER
    `bootstrap_domains` returned, so a write crash would have swallowed it entirely."""
    graph = _write(tmp_path, "g.json", _many_communities_graph(150))
    db = tmp_path / "t.db"

    def _boom(self, domain):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr("sidegraph.store.Store.add_domain", _boom)

    with pytest.raises(RuntimeError):
        domains_main(["bootstrap", "--db", str(db), "--graph", str(graph)])

    err = capsys.readouterr().err
    assert "150 significant communities found" in err
    assert "showing only the top 100" in err


# -- add ------------------------------------------------------------------------------


def test_cli_domains_add_lands_proposed(tmp_path, capsys):
    db = tmp_path / "t.db"
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "payments",
                "--title",
                "Payments",
                "--summary",
                "Handles order settlement and refunds.",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "proposed 1 domain(s) (skipped: 0 existing)" in out

    store = Store(db)
    d = store.find_domain_by_slug("payments")
    assert d is not None
    assert d.status == DomainStatus.PROPOSED
    assert d.title == "Payments"
    assert d.provenance.source == "manual"


def test_cli_domains_add_with_parent_and_paths(tmp_path, capsys):
    db = tmp_path / "t.db"
    domains_main(
        [
            "add",
            "--db",
            str(db),
            "--slug",
            "root",
            "--title",
            "Root",
            "--summary",
            "Root area.",
        ]
    )
    capsys.readouterr()
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "child",
                "--title",
                "Child",
                "--summary",
                "Child area.",
                "--parent",
                "root",
                "--path",
                "src/child",
                "--path",
                "lib/child",
            ]
        )
        == 0
    )

    store = Store(db)
    root = store.find_domain_by_slug("root")
    child = store.find_domain_by_slug("child")
    assert child.parent_id == root.domain_id
    assert child.path_prefixes == ["src/child", "lib/child"]


def test_cli_domains_add_unresolvable_parent_exits_one(tmp_path, capsys):
    db = tmp_path / "t.db"
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "child",
                "--title",
                "Child",
                "--summary",
                "s",
                "--parent",
                "no-such-slug",
            ]
        )
        == 1
    )
    assert "does not resolve" in capsys.readouterr().out


def test_cli_domains_add_slug_collision_reported_as_skip(tmp_path, capsys):
    db = tmp_path / "t.db"
    domains_main(
        [
            "add",
            "--db",
            str(db),
            "--slug",
            "payments",
            "--title",
            "Payments",
            "--summary",
            "s",
        ]
    )
    capsys.readouterr()
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "payments",
                "--title",
                "Payments Again",
                "--summary",
                "s2",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "proposed 0 domain(s) (skipped: 1 existing" in out


def test_cli_domains_add_invalid_slug_exits_one(tmp_path, capsys):
    db = tmp_path / "t.db"
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "Not_A_Slug",
                "--title",
                "Bad",
                "--summary",
                "s",
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "error" in out
    assert not db.exists()


def test_cli_domains_add_store_open_failure_exits_one(tmp_path, capsys):
    db = tmp_path / "bad.db"
    Store(db)
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db — corrupt THAT file, not `db` itself (see test above).
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert (
        domains_main(
            [
                "add",
                "--db",
                str(db),
                "--slug",
                "payments",
                "--title",
                "Payments",
                "--summary",
                "s",
            ]
        )
        == 1
    )
    assert "not readable" in capsys.readouterr().out


# -- ratify schedules a heal when membership could not be resolved ----------------------


def _proposed_domain(store, slug: str, **kw):
    """A status=proposed domain, ready to ratify. Defaults to one seed anchor so ratify has
    something to resolve; pass path_prefixes=/seed_anchors= to override."""
    kw.setdefault("seed_anchors", [{"name": "foo()", "file_path": "a.py"}])
    return store.add_domain(
        Domain(
            slug=slug,
            title=slug,
            summary=f"{slug} summary",
            status=DomainStatus.PROPOSED,
            provenance=Provenance(source="manual"),
            **kw,
        )
    )


def test_cli_ratify_of_an_accepted_domain_schedules_a_heal(tmp_path):
    """cli.ratify_main never constructs a reader, so a CLI-ratified domain gets no
    immediate membership resolution at all. The flag is what makes the next sync heal it
    instead of skipping."""
    from sidegraph.store import VOLATILE_STALE_KEY

    store = Store(tmp_path / "t.db")
    domain = _proposed_domain(store, slug="cli-ratified")
    store.set_meta(VOLATILE_STALE_KEY, "0")

    ratify_main(["--db", str(tmp_path / "t.db"), "--accept", domain.domain_id])

    assert Store(tmp_path / "t.db").get_meta(VOLATILE_STALE_KEY) == "1"


def test_cli_dropping_a_domain_does_NOT_schedule_a_heal(tmp_path):
    """A dropped domain needs no membership resolution, so it must not schedule a full
    ladder pass. This is why the flag cannot ride on `domain_changed`, which the drop path
    sets too (cli.py:372)."""
    from sidegraph.store import VOLATILE_STALE_KEY

    store = Store(tmp_path / "t.db")
    domain = _proposed_domain(store, slug="cli-dropped")
    store.set_meta(VOLATILE_STALE_KEY, "0")

    ratify_main(["--db", str(tmp_path / "t.db"), "--drop", domain.domain_id])

    assert Store(tmp_path / "t.db").get_meta(VOLATILE_STALE_KEY) == "0"
