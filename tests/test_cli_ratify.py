import json
import sqlite3

from sidegraph.cli import ratify_main
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.schema import DecisionStatus, Descriptor, Domain, DomainStatus, Provenance
from sidegraph.server import _add_domain_impl, _propose_decisions_impl
from sidegraph.store import Store


def _seed(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    results = _propose_decisions_impl(
        store,
        None,
        [
            {
                "title": "T1",
                "kind": "lesson",
                "context": "c",
                "choice": "ch",
                "anchors": [],
            }
        ],
    )
    return db, store, results[0]["decision_id"]


def test_cli_lists_pending(tmp_path, capsys):
    db, _, did = _seed(tmp_path)
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert did in out and "T1" in out and "pending" in out


def test_cli_accept_flips_status(tmp_path, capsys):
    db, store, did = _seed(tmp_path)
    assert ratify_main(["--db", str(db), "--accept", did]) == 0
    assert store.get_decision(did).status == DecisionStatus.ACCEPTED
    assert f"accepted {did}" in capsys.readouterr().out


def test_cli_drop_rejects(tmp_path, capsys):
    db, store, did = _seed(tmp_path)
    assert ratify_main(["--db", str(db), "--drop", did]) == 0
    assert store.get_decision(did).status == DecisionStatus.REJECTED


def test_cli_all_accepts_everything(tmp_path, capsys):
    db, store, did = _seed(tmp_path)
    assert ratify_main(["--db", str(db), "--all"]) == 0
    assert store.get_decision(did).status == DecisionStatus.ACCEPTED


def test_cli_error_on_unknown_id(tmp_path, capsys):
    db, _, _ = _seed(tmp_path)
    assert ratify_main(["--db", str(db), "--accept", "nope"]) == 1
    assert "error nope" in capsys.readouterr().out


def test_cli_partial_failure_still_processes_the_rest(tmp_path, capsys):
    db, store, did = _seed(tmp_path)
    assert ratify_main(["--db", str(db), "--accept", did, "nope"]) == 1
    out = capsys.readouterr().out
    assert f"accepted {did}" in out and "error nope" in out
    assert store.get_decision(did).status == DecisionStatus.ACCEPTED


def test_cli_store_open_failure_exits_nonzero(tmp_path, capsys):
    db = tmp_path / "bad.db"
    Store(db)  # create a valid store first
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db (git-native store) — corrupt THAT file, not `db` itself.
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert ratify_main(["--db", str(db)]) == 1
    assert "not readable" in capsys.readouterr().out


# -- domains: listing sections + accept/drop through the routing impl (mind-model layer M2)


def _seed_domain(tmp_path, db=None, slug="payments"):
    db = db or (tmp_path / "t.db")
    store = Store(db)
    out = _add_domain_impl(store, None, slug=slug, title=slug.title(), summary="s.")
    return db, store, out["domain_id"]


def test_cli_lists_pending_domains_sectioned(tmp_path, capsys):
    db, _, domain_id = _seed_domain(tmp_path)
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Domains:" in out and domain_id in out and "Payments" in out


# -- Gate-5 fix 3: the ratify listing shows the membership rule (path_prefixes) and seed
# communities, so a human can catch an over-broad rule (previously invisible here).


def test_cli_domain_listing_shows_path_prefixes_and_communities(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s.",
            communities=["1", "2", "3"],
            path_prefixes=["payments", "billing"],
            provenance=Provenance(source="manual"),
        )
    )
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "paths:" in out and "payments/" in out and "billing/" in out
    assert "communities:" in out and "1" in out and "2" in out and "3" in out


def test_cli_domain_listing_shows_none_when_rule_empty(tmp_path, capsys):
    """Even an EMPTY path_prefixes/communities is rendered explicitly ("(none)") rather
    than the line vanishing — a human should be able to see there's no membership rule at
    all, not just infer it from the summary prose."""
    db = tmp_path / "t.db"
    store = Store(db)
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s.",
            provenance=Provenance(source="manual"),
        )
    )
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "paths:   (none)" in out
    assert "communities: (none)" in out


def test_cli_domain_listing_truncates_large_communities_sample(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    many = [str(i) for i in range(30)]
    store.add_domain(
        Domain(
            slug="big",
            title="Big",
            summary="s.",
            communities=many,
            provenance=Provenance(source="manual"),
        )
    )
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    communities_line = next(line for line in out.splitlines() if "communities:" in line)
    # Scoped to the communities line alone: the domain_id printed elsewhere in the same
    # listing is a ULID and can itself contain the substring "29", which made this flaky
    # when checked against the whole output.
    assert "more" in communities_line  # truncated, not all 30 ids dumped raw
    assert "29" not in communities_line  # the tail of the (truncated) sample never appears


# -- Gate-6 finding: the ratify listing was BLIND to seed_anchors -- the name-domains
# skill's PRIMARY membership shape -- so a domain drafted with ONLY seed_anchors rendered
# as "paths: (none)  communities: (none)", hiding the very rule the human is meant to
# ratify. See ``capture.format_seed_anchors_sample``.


def test_cli_domain_listing_shows_seed_anchors_when_paths_and_communities_empty(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s.",
            seed_anchors=[Descriptor(name="OrderBook", file_path="trader/order_book.py")],
            provenance=Provenance(source="manual"),
        )
    )
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    # paths/communities still render explicitly...
    assert "paths:   (none)" in out
    assert "communities: (none)" in out
    # ...but the anchors line now shows the actual rule being ratified, not just "(none)".
    assert "anchors:" in out
    assert "OrderBook@trader/order_book.py" in out


def test_cli_domain_listing_omits_anchors_line_when_seed_anchors_empty(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s.",
            provenance=Provenance(source="manual"),
        )
    )
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "anchors:" not in out


def test_cli_domain_listing_truncates_large_seed_anchors_sample(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    many = [Descriptor(name=f"E{i}", file_path=f"a/e{i}.py") for i in range(5)]
    store.add_domain(
        Domain(
            slug="big",
            title="Big",
            summary="s.",
            seed_anchors=many,
            provenance=Provenance(source="manual"),
        )
    )
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    anchors_line = next(line for line in out.splitlines() if "anchors:" in line)
    assert "more" in anchors_line
    assert "E4@a/e4.py" not in anchors_line  # tail of the (truncated) sample never appears


def test_cli_lists_both_decisions_and_domains_sectioned(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    dec = _propose_decisions_impl(
        store,
        None,
        [
            {
                "title": "T1",
                "kind": "lesson",
                "context": "c",
                "choice": "ch",
                "anchors": [],
            }
        ],
    )
    did = dec[0]["decision_id"]
    domain_out = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = domain_out["domain_id"]

    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Decisions:" in out and did in out
    assert "Domains:" in out and domain_id in out
    assert "2 pending" in out


def test_cli_no_pending_decisions_or_domains_message(tmp_path, capsys):
    db = tmp_path / "empty.db"
    Store(db)
    assert ratify_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "No proposed decisions" in out and "domains" in out


def test_cli_accept_a_domain_id(tmp_path, capsys):
    db, store, domain_id = _seed_domain(tmp_path)
    assert ratify_main(["--db", str(db), "--accept", domain_id]) == 0
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED
    assert f"accepted {domain_id}" in capsys.readouterr().out


def test_cli_drop_a_domain_id(tmp_path, capsys):
    db, store, domain_id = _seed_domain(tmp_path)
    assert ratify_main(["--db", str(db), "--drop", domain_id]) == 0
    assert store.get_domain(domain_id).status == DomainStatus.DROPPED


def test_cli_drop_an_already_accepted_domain_id(tmp_path, capsys):
    """Review round 3 fix 3: retiring an already-ACCEPTED domain via --drop (design §6's
    resolution path for a cross-branch accepted-vs-accepted slug conflict, which
    supersede_domain cannot resolve on its own)."""
    db, store, domain_id = _seed_domain(tmp_path)
    store.ratify_domains(accept=[domain_id])
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED

    assert ratify_main(["--db", str(db), "--drop", domain_id]) == 0
    assert store.get_domain(domain_id).status == DomainStatus.DROPPED
    assert f"dropped {domain_id}" in capsys.readouterr().out


def test_cli_all_accepts_both_decisions_and_domains(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    dec = _propose_decisions_impl(
        store,
        None,
        [
            {
                "title": "T1",
                "kind": "lesson",
                "context": "c",
                "choice": "ch",
                "anchors": [],
            }
        ],
    )
    did = dec[0]["decision_id"]
    domain_out = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = domain_out["domain_id"]

    assert ratify_main(["--db", str(db), "--all"]) == 0
    assert store.get_decision(did).status == DecisionStatus.ACCEPTED
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED


# -- ratify activates the TOC cache immediately (fix: lazy sync alone never rebuilds it) --


def test_cli_accept_domain_id_refreshes_toc_cache(tmp_path, capsys):
    db, store, domain_id = _seed_domain(tmp_path)
    assert store.get_meta(TOC_CACHE_KEY) is None

    assert ratify_main(["--db", str(db), "--accept", domain_id]) == 0

    cached = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert any(d["slug"] == "payments" for d in cached["domains"])


def test_cli_drop_domain_id_refreshes_toc_cache(tmp_path, capsys):
    db, store, domain_id = _seed_domain(tmp_path)
    assert store.get_meta(TOC_CACHE_KEY) is None

    assert ratify_main(["--db", str(db), "--drop", domain_id]) == 0

    # dropped, not accepted -> never listed in the TOC, but the cache IS rebuilt (the
    # domain that was proposed a moment ago is gone from the count either way)
    cached = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert cached["domains"] == []


def test_cli_decisions_only_ratify_leaves_toc_cache_untouched(tmp_path, capsys):
    db, store, did = _seed(tmp_path)
    store.set_meta(TOC_CACHE_KEY, "sentinel")

    assert ratify_main(["--db", str(db), "--accept", did]) == 0

    assert store.get_meta(TOC_CACHE_KEY) == "sentinel"


def test_cli_mixed_accept_decision_and_domain_ids(tmp_path, capsys):
    db = tmp_path / "t.db"
    store = Store(db)
    dec = _propose_decisions_impl(
        store,
        None,
        [
            {
                "title": "T1",
                "kind": "lesson",
                "context": "c",
                "choice": "ch",
                "anchors": [],
            }
        ],
    )
    did = dec[0]["decision_id"]
    domain_out = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = domain_out["domain_id"]

    assert ratify_main(["--db", str(db), "--accept", did, domain_id]) == 0
    out = capsys.readouterr().out
    assert f"accepted {did}" in out and f"accepted {domain_id}" in out
    assert store.get_decision(did).status == DecisionStatus.ACCEPTED
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED


# -- --db resolution (bare run, no --db / no env): see config.resolve_store_path. Full
# precedence-matrix coverage (explicit > SIDEGRAPH_DIR > SIDEGRAPH_DB (deprecated,
# dispatched) > existing .sidegraph/ > default .sidegraph) lives in tests/test_config.py;
# these are the CLI-level integration checks that ratify_main actually wires through it.


def _propose(store, title):
    results = _propose_decisions_impl(
        store,
        None,
        [
            {
                "title": title,
                "kind": "lesson",
                "context": "c",
                "choice": "ch",
                "anchors": [],
            }
        ],
    )
    return results[0]["decision_id"]


def _reset_deprecation_warning(monkeypatch):
    import sidegraph.config as config

    monkeypatch.setattr(config, "_deprecation_warned", False)


def test_cli_ratify_bare_prefers_existing_dot_sidegraph_store(tmp_path, capsys, monkeypatch):
    dot_dir = tmp_path / ".sidegraph"
    store = Store(dot_dir)  # ".sidegraph" IS the store now, not a nested "decisions.db"
    did = _propose(store, "InDotSidegraph")

    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ratify_main([]) == 0
    captured = capsys.readouterr()
    assert did in captured.out and "InDotSidegraph" in captured.out
    assert captured.err == ""  # store already existed -> no warning


def test_cli_ratify_bare_creates_default_dot_sidegraph_with_warning(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ratify_main([]) == 0
    captured = capsys.readouterr()
    assert "No proposed decisions" in captured.out
    assert (tmp_path / ".sidegraph" / "format").is_file()
    assert "warning: creating new store at .sidegraph" in captured.err
    assert "--db or set SIDEGRAPH_DIR" in captured.err


def test_cli_ratify_sidegraph_dir_wins_over_dot_sidegraph_and_sidegraph_db(
    tmp_path, capsys, monkeypatch
):
    dot_dir = tmp_path / ".sidegraph"
    Store(dot_dir)  # exists but must be ignored -- SIDEGRAPH_DIR wins

    via_db_env = tmp_path / "via-db-env.db"
    Store(via_db_env)  # exists but must be ignored -- SIDEGRAPH_DIR wins over SIDEGRAPH_DB too

    dir_env = tmp_path / "via-dir-env"
    did = _propose(Store(dir_env), "ViaDirEnv")

    monkeypatch.setenv("SIDEGRAPH_DIR", str(dir_env))
    monkeypatch.setenv("SIDEGRAPH_DB", str(via_db_env))
    monkeypatch.chdir(tmp_path)
    assert ratify_main([]) == 0
    captured = capsys.readouterr()
    assert did in captured.out and "ViaDirEnv" in captured.out
    assert captured.err == ""  # SIDEGRAPH_DIR wins outright -- no deprecation note either


def test_cli_ratify_sidegraph_db_back_compat_wins_over_dot_sidegraph(tmp_path, capsys, monkeypatch):
    """SIDEGRAPH_DB is deprecated but still honored (design §5) -- and still takes
    precedence over an existing ``.sidegraph/`` when SIDEGRAPH_DIR is unset."""
    _reset_deprecation_warning(monkeypatch)
    dot_dir = tmp_path / ".sidegraph"
    Store(dot_dir)  # exists but must be ignored -- SIDEGRAPH_DB wins

    custom_db = tmp_path / "custom.db"
    did = _propose(Store(custom_db), "ViaEnv")

    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(custom_db))
    monkeypatch.chdir(tmp_path)
    assert ratify_main([]) == 0
    captured = capsys.readouterr()
    assert did in captured.out and "ViaEnv" in captured.out
    assert "SIDEGRAPH_DB is deprecated" in captured.err
    assert "creating new store" not in captured.err  # store already existed -> no such warning


def test_cli_ratify_explicit_db_wins_over_everything(tmp_path, capsys, monkeypatch):
    dot_dir = tmp_path / ".sidegraph"
    Store(dot_dir)
    Store(tmp_path / "dir-env")
    Store(tmp_path / "env.db")

    explicit_db = tmp_path / "explicit.db"
    did = _propose(Store(explicit_db), "ViaExplicit")

    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "dir-env"))
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "env.db"))
    monkeypatch.chdir(tmp_path)
    assert ratify_main(["--db", str(explicit_db)]) == 0
    captured = capsys.readouterr()
    assert did in captured.out and "ViaExplicit" in captured.out
    assert captured.err == ""


def test_cli_ratify_resolves_domain_membership_immediately(tmp_path, monkeypatch):
    """CLI/MCP ratify parity (v0.2-scope): the MCP `ratify` tool resolves an accepted
    domain's membership straight away, while `sidegraph-ratify` left `communities: []`
    until the next sync — the same accept through two doors gave different state."""
    import sidegraph.cli as cli_mod
    import sidegraph.sync as sync_mod

    db, store, _ = _seed(tmp_path)
    dom = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="Settlement.",
        communities=[],
        path_prefixes=["src/pay/"],
    )
    domain_id = dom["domain_id"]

    calls: list[str] = []

    def fake_refresh(domain, store_, reader):
        calls.append(domain.domain_id)
        return (["c-1"], None)

    # Task 4 (spec C-10): the resolve call now happens inside sync.activate_accepted_domain
    # (extracted from cli.ratify_main), so the patch target moves from `cli_mod` to
    # `sync_mod` -- a patch on `cli_mod` sits off the call path after the extraction.
    monkeypatch.setattr(sync_mod, "refresh_domain_communities_now", fake_refresh)
    monkeypatch.setattr(cli_mod, "_ratify_reader", lambda *a, **k: object())  # graph present

    assert ratify_main(["--db", str(db), "--accept", domain_id]) == 0
    assert calls == [domain_id]


def test_cli_ratify_without_a_graph_still_schedules_the_heal(tmp_path, monkeypatch):
    """No reader — the pre-existing behaviour must survive: accept, then flag the store so
    the next reload heals membership instead of the domain never gaining any."""
    import sidegraph.cli as cli_mod
    from sidegraph.store import VOLATILE_STALE_KEY

    db, store, _ = _seed(tmp_path)
    dom = _add_domain_impl(
        store, None, slug="payments", title="Payments", summary="S.", communities=[]
    )
    monkeypatch.setattr(cli_mod, "_ratify_reader", lambda *a, **k: None)
    store.set_meta(VOLATILE_STALE_KEY, "0")  # fresh stores start at "1" — reset or vacuous
    assert ratify_main(["--db", str(db), "--accept", dom["domain_id"]]) == 0
    assert Store(db).get_meta(VOLATILE_STALE_KEY) == "1"


def test_cli_ratify_ignores_a_graph_belonging_to_another_project(tmp_path, monkeypatch):
    """A relative --graph resolves against the STORE's project root, never the shell's CWD:
    ratifying a store elsewhere must not resolve its domains against whatever graph happens
    to sit beside the terminal."""
    import os

    from sidegraph.store import VOLATILE_STALE_KEY

    elsewhere = tmp_path / "other-project"
    (elsewhere / "graphify-out").mkdir(parents=True)
    (elsewhere / "graphify-out" / "graph.json").write_text('{"nodes": [], "edges": []}')
    db, store, _ = _seed(tmp_path)
    dom = _add_domain_impl(
        store, None, slug="payments", title="Payments", summary="S.", communities=[]
    )
    store.set_meta(VOLATILE_STALE_KEY, "0")  # fresh stores start at "1" — reset or vacuous
    cwd = os.getcwd()
    os.chdir(elsewhere)
    try:
        assert ratify_main(["--db", str(db), "--accept", dom["domain_id"]]) == 0
    finally:
        os.chdir(cwd)
    assert Store(db).get_meta(VOLATILE_STALE_KEY) == "1"


def test_cli_ratify_resolves_every_domain_even_if_one_fails(tmp_path, monkeypatch):
    """Review finding 6b: a `break` on the first failure left later domains with
    `communities: []` until the next sync — the exact UX asymmetry this parity fix closes,
    reintroduced on the error path."""
    import sidegraph.cli as cli_mod
    import sidegraph.sync as sync_mod
    from sidegraph.store import VOLATILE_STALE_KEY

    db, store, _ = _seed(tmp_path)
    ids = []
    for slug in ("alpha", "beta", "gamma"):
        dom = _add_domain_impl(
            store, None, slug=slug, title=slug.title(), summary="S.", communities=[]
        )
        ids.append(dom["domain_id"])

    attempted: list[str] = []

    def flaky(domain, store_, reader):
        attempted.append(domain.slug)
        if domain.slug == "beta":
            raise RuntimeError("resolve blew up")
        return (["c-1"], None)

    # Task 4 (spec C-10): patch target moves to sync_mod -- see the "resolves domain
    # membership immediately" test above for why.
    monkeypatch.setattr(sync_mod, "refresh_domain_communities_now", flaky)
    monkeypatch.setattr(cli_mod, "_ratify_reader", lambda *a, **k: object())
    store.set_meta(VOLATILE_STALE_KEY, "0")

    assert ratify_main(["--db", str(db), "--accept", *ids]) == 0
    assert attempted == ["alpha", "beta", "gamma"]  # not stopped at the failure
    assert Store(db).get_meta(VOLATILE_STALE_KEY) == "1"  # and the heal is still scheduled


def test_cli_ratify_prints_the_overbroad_path_rule_like_mcp_does(tmp_path, monkeypatch, capsys):
    """Review finding 6a: "path rule too broad" is a literal trigger phrase in the
    sidegraph:heal-anchors skill; the MCP path returns it and the CLI dropped it, so the
    same accept through two doors did not say the same thing."""
    import sidegraph.cli as cli_mod
    import sidegraph.sync as sync_mod

    db, store, _ = _seed(tmp_path)
    dom = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="S.",
        communities=[],
        path_prefixes=["src/"],
    )
    # Task 4 (spec C-10): patch target moves to sync_mod -- see the "resolves domain
    # membership immediately" test above for why.
    monkeypatch.setattr(
        sync_mod,
        "refresh_domain_communities_now",
        lambda d, s, r: ([], {"matched": 82, "total": 334}),
    )
    monkeypatch.setattr(cli_mod, "_ratify_reader", lambda *a, **k: object())

    assert ratify_main(["--db", str(db), "--accept", dom["domain_id"]]) == 0
    out = capsys.readouterr().out
    assert "path rule too broad: 'src/' match 82/334 communities" in out


def test_ratify_reader_resolves_the_project_root_across_store_layouts(tmp_path):
    """Review R2-4: a name check on ".sidegraph" resolved a custom store directory's root
    to ITSELF, silently disabling the immediate resolve for anyone not on the default
    directory name — `--db mystore` is a supported invocation. The rule is structural: the
    root is the store path's parent, one level further only for a legacy single FILE
    sitting inside a store directory."""
    import sidegraph.cli as cli_mod

    cases = []
    # canonical directory store
    proj = tmp_path / "p1"
    (proj / ".sidegraph").mkdir(parents=True)
    cases.append((proj / ".sidegraph", proj))
    # custom directory store — the row that regressed
    proj2 = tmp_path / "p2"
    (proj2 / "mystore").mkdir(parents=True)
    cases.append((proj2 / "mystore", proj2))
    # nested custom directory store
    proj3 = tmp_path / "p3"
    (proj3 / "meta" / "store").mkdir(parents=True)
    cases.append((proj3 / "meta" / "store", proj3 / "meta"))
    # legacy single file at the project root
    proj4 = tmp_path / "p4"
    proj4.mkdir()
    (proj4 / "sidegraph.db").write_text("")
    cases.append((proj4 / "sidegraph.db", proj4))
    # legacy single file INSIDE a store directory
    proj5 = tmp_path / "p5"
    (proj5 / ".sidegraph").mkdir(parents=True)
    (proj5 / ".sidegraph" / "decisions.db").write_text("")
    cases.append((proj5 / ".sidegraph" / "decisions.db", proj5))

    for db_path, expected_root in cases:
        graph_dir = expected_root / "graphify-out"
        graph_dir.mkdir(parents=True, exist_ok=True)
        (graph_dir / "graph.json").write_text('{"nodes": [], "edges": []}')
        assert cli_mod._ratify_reader("graphify-out/graph.json", db_path) is not None, db_path
