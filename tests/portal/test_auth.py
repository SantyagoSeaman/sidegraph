"""Authorization, event transport and read-only HTTP behavior."""

import mimetypes
import os
import signal
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sidegraph.portal import contract
from sidegraph.schema import AnchorBinding, Entity
from sidegraph.store import Store, _has_existing_store
from tests.portal import test_launch
from tests.store_record_support import _decision

_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _error(response, code: str, status: int | None = None) -> None:
    assert response.status_code == (status or contract.ERROR_CODES[code]), response.text
    assert contract.ErrorBody.model_validate(response.json()).error.code == code
    for name, expected in _HEADERS.items():
        assert response.headers[name] == expected


def _ticket(client: TestClient) -> str:
    return contract.LiveTicket.model_validate(client.post("/api/live-ticket").json()).ticket


def test_unknown_token_is_401_not_500(portal_ctx, portal_client) -> None:
    with portal_client(portal_ctx()) as client:
        client.headers.pop("X-Sidegraph-Token")
        _error(client.post("/api/live-ticket"), "unauthorized")
        client.headers["X-Sidegraph-Token"] = "old-token"
        _error(client.post("/api/live-ticket"), "unauthorized")
        response = client.post("/api/live-ticket", headers=[(b"X-Sidegraph-Token", b"\xff")])
        _error(response, "unauthorized")


@pytest.mark.parametrize("host", ["localhost:54321", "rebound.example:54321"])
def test_bad_host(portal_ctx, portal_client, host: str) -> None:
    with portal_client(portal_ctx()) as client:
        _error(client.get("/", headers={"Host": host}), "bad_host")


def test_post_needs_matching_origin(portal_ctx, portal_client) -> None:
    with portal_client(portal_ctx()) as client:
        _error(
            client.post("/api/live-ticket", headers={"Origin": "http://example.test"}), "bad_origin"
        )
        client.headers.pop("Origin")
        _error(client.post("/api/live-ticket"), "bad_origin")


def test_live_requires_fresh_ticket(
    portal_ctx, portal_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sidegraph.portal import live

    now = 10.0
    monkeypatch.setattr(live, "monotonic", lambda: now)
    with portal_client(portal_ctx()) as client:
        expired = _ticket(client)
        now += 30.0
        fresh = _ticket(client)
        client.headers.pop("Origin")
        client.headers.pop("X-Sidegraph-Token")
        _error(client.get("/live"), "unauthorized")
        _error(client.get("/live", params={"ticket": expired}), "unauthorized")
        _error(
            client.get(
                "/live", params={"ticket": fresh}, headers={"Origin": "http://example.test"}
            ),
            "bad_origin",
        )
        with client.stream("GET", "/live", params={"ticket": fresh}) as response:
            assert response.status_code == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            assert response.headers["Referrer-Policy"] == "no-referrer"
        _error(client.get("/live", params={"ticket": fresh}), "unauthorized")


@pytest.mark.slow
@test_launch._posix
def test_closing_event_on_shutdown(portal_ctx) -> None:
    ctx = portal_ctx()
    with test_launch._child("--db", str(ctx.store_path)) as child:
        origin, token = test_launch._url(child)
        with test_launch._stream(origin, token) as response:
            child.send_signal(signal.SIGINT)
            assert "event: closing" in list(response.iter_lines())
        assert child.wait(timeout=2) == 0


def test_public_shell_assets_and_method_error(portal_ctx, portal_client) -> None:
    with portal_client(portal_ctx()) as client:
        client.headers.pop("X-Sidegraph-Token")
        for path in ("/", contract.VIS_NETWORK_PATH):
            response = client.get(path)
            assert response.status_code == 200
            assert "Access-Control-Allow-Origin" not in response.headers
            for name, expected in _HEADERS.items():
                assert response.headers[name] == expected
        response = client.post("/")
        _error(response, "invalid", 405)
        assert response.headers["Allow"] == "GET"
        _error(client.get("/static/missing.js"), "not_found")


@pytest.mark.parametrize("path", ["/static/app.js", "/static/vendor/preact.mjs"])
def test_static_modules_have_javascript_content_type(
    portal_ctx, portal_client, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(mimetypes, "_db", mimetypes.MimeTypes())
    mimetypes.add_type("text/plain", Path(path).suffix)
    with portal_client(portal_ctx()) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Content-Type"].split(";", 1)[0] == "text/javascript"


def test_two_portals_independent(portal_ctx, portal_client) -> None:
    with (
        portal_client(portal_ctx(token="first", port=54321)) as first,
        portal_client(portal_ctx(token="second", port=54322)) as second,
    ):
        assert _ticket(first) != _ticket(second)
        _error(
            first.post("/api/live-ticket", headers={"X-Sidegraph-Token": "second"}), "unauthorized"
        )
        assert _ticket(first)


def test_only_authenticated_user_activity_resets_idle_clock(portal_ctx, portal_client) -> None:
    with portal_client(portal_ctx()) as client:
        started = client.app.state.last_user_activity()
        client.get("/")
        _error(
            client.post(
                "/api/live-ticket",
                headers={
                    "X-Sidegraph-Activity": "user",
                    "X-Sidegraph-Token": "old",
                },
            ),
            "unauthorized",
        )
        _ticket(client)
        assert client.app.state.last_user_activity() == started
        response = client.post("/api/live-ticket", headers={"X-Sidegraph-Activity": "user"})
        assert response.status_code == 200
        assert client.app.state.last_user_activity() > started


def test_no_get_route_writes(portal_ctx, portal_client) -> None:
    from tests.portal.test_packaging import _pending

    ctx = portal_ctx()
    pending = _pending(ctx)
    with Store(ctx.store_path) as store:
        decision = store.add_decision(_decision())
        entity = store.upsert_entity(Entity(canonical_name="existing anchor"))
        store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=0))
        store.record_retrieval_events("existing-journal-entry", ["seed"], [])
    with closing(sqlite3.connect(ctx.store_path / "index.db")) as db:
        db.execute("ALTER TABLE retrieval_events DROP COLUMN agent")

    def snapshot() -> tuple[dict[Path, tuple[bytes, int]], dict]:
        files = {
            p.relative_to(ctx.store_path): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in ctx.store_path.rglob("*")
            if p.is_file() and not p.name.startswith("index.db")
        }
        with closing(sqlite3.connect(ctx.store_path / "index.db")) as db:
            journal = {
                table: db.execute(f"SELECT * FROM {table}").fetchall()
                for table in Store._JOURNAL_TABLES
            }
        return files, journal

    before = snapshot()
    token_header = {"X-Sidegraph-Token": ctx.token}
    with portal_client(ctx) as client:
        origin = client.headers.pop("Origin")
        client.headers.pop("X-Sidegraph-Token")
        for route in contract.ROUTES:
            if route.method != "GET" or route.module in pending:
                continue
            if route.auth == "ticket":
                minted = client.post("/api/live-ticket", headers={**token_header, "Origin": origin})
                ticket = contract.LiveTicket.model_validate(minted.json()).ticket
                with client.stream("GET", route.path, params={"ticket": ticket}) as response:
                    assert response.status_code == 200
                assert snapshot() == before
                continue
            path = route.path.format(
                id=decision.id, entity_id=entity.entity_id, path="vendor/vis-network.min.js"
            )
            response = client.get(
                path,
                headers=token_header if route.auth == "token" else {},
                params={"q": decision.title} if route.path == "/api/search" else None,
            )
            if route.path.endswith("/snippet"):
                _error(response, "refused")
                assert response.json()["error"]["details"]["reason"] == "no_repository_root"
            else:
                assert response.status_code == 200, path
            assert snapshot() == before


@pytest.mark.parametrize("unrelated_json", [False, True])
def test_cleared_store_directory_is_not_reinitialized(
    portal_ctx, portal_client, tmp_path: Path, unrelated_json: bool
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    if unrelated_json:
        folder = empty / "entities"
        folder.mkdir()
        (folder / "catalog.json").write_text('{"currency":"GBP"}')
    before = set(empty.rglob("*"))
    ctx = replace(portal_ctx(), store_path=empty)
    with portal_client(ctx, raise_server_exceptions=False) as client:
        _error(client.get("/api/session"), "not_found")
    with pytest.raises(FileNotFoundError):
        ctx.poller_store()
    assert set(empty.rglob("*")) == before


def test_read_route_busy_503(portal_ctx, portal_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from sidegraph.portal import api_read

    ctx = portal_ctx()

    def read(_request):
        with ctx.request_store() as store:
            store.refresh_if_stale()
        return JSONResponse({})

    monkeypatch.setattr(api_read, "routes", lambda _ctx: [Route("/api/graph", read)])
    with portal_client(ctx) as client, sqlite3.connect(ctx.store_path / "index.db") as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        _error(client.get("/api/graph"), "busy")
        assert 1.8 <= time.monotonic() - started < 4
        writer.rollback()


def test_session_reports_an_unsupported_store_format(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with portal_client(ctx, raise_server_exceptions=False) as client:
        assert client.get("/api/session").status_code == 200
        (ctx.store_path / "format").write_text("sidegraph-store 999.0.0\n")
        response = client.get("/api/session")
        _error(response, "refused")
        assert response.json()["error"]["details"]["reason"] == "store_format"


@pytest.mark.parametrize("handle", ["request_store", "poller_store"])
@pytest.mark.parametrize("replacement", [False, True], ids=["absent", "different-store"])
def test_store_handle_refuses_root_moved_during_open(
    portal_ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handle: str, replacement: bool
) -> None:
    ctx = portal_ctx()
    moved = tmp_path / "moved"
    check_requirement = Store._check_requirement

    def move_after_check(store: Store, *, force: bool = False) -> str | None:
        requirement = check_requirement(store, force=force)
        if not moved.exists():
            store.path.rename(moved)
            if replacement:
                store.path.mkdir()
                (store.path / "format").write_bytes((moved / "format").read_bytes())
        return requirement

    monkeypatch.setattr(Store, "_check_requirement", move_after_check)
    with (
        pytest.raises(FileNotFoundError, match="store moved or missing"),
        getattr(ctx, handle)(),
    ):
        pass

    assert moved.is_dir()
    if replacement:
        assert [entry.name for entry in ctx.store_path.iterdir()] == ["format"]
    else:
        assert not (ctx.store_path / "format").exists()
        assert not ctx.store_path.exists()


def test_session_reports_missing_format_without_backfilling(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        store.add_decision(_decision())
    marker = ctx.store_path / "format"
    marker.unlink()

    with portal_client(ctx) as client:
        response = client.get("/api/session")

    assert not marker.exists()
    _error(response, "refused")
    error = response.json()["error"]
    assert error["details"]["reason"] == "store_format"
    assert "format marker missing" in error["message"]


def test_empty_derived_index_is_not_a_store(
    portal_ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sidegraph import server
    from tests.test_bounded_listings import call

    root = tmp_path / "index-only"
    root.mkdir()
    index = root / "index.db"
    index.touch()
    ctx = replace(portal_ctx(), store_path=root)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server, "_store", None)

    assert not _has_existing_store(root)
    for open_handle in (ctx.request_store, ctx.poller_store):
        with pytest.raises(FileNotFoundError), open_handle():
            pass
    for name, arguments in (
        ("advise_ratification", {"items": []}),
        ("open_portal", {"open_browser": False}),
    ):
        result = call(name, arguments)
        assert result.is_error
        assert "no store" in result.content[0].text
    assert list(root.iterdir()) == [index]
    assert index.stat().st_size == 0


def test_session_reports_that_the_store_is_missing(portal_ctx, portal_client, tmp_path: Path):
    ctx = portal_ctx()
    ctx.store_path.rename(tmp_path / "moved")
    with portal_client(ctx) as client:
        response = client.get("/api/session")

    _error(response, "not_found")
    assert response.json()["error"]["message"] == "The store moved or is missing."


def test_session_reports_non_database_store_path(portal_ctx, portal_client, tmp_path: Path):
    ctx = portal_ctx()
    ctx.store_path.rename(tmp_path / "moved")
    ctx.store_path.write_text("plain text\n")
    with portal_client(ctx, raise_server_exceptions=False) as client:
        response = client.get("/api/session")

    _error(response, "not_found")


@pytest.mark.parametrize("marker", [False, True])
def test_session_reports_plain_text_legacy_filename(portal_ctx, portal_client, marker: bool):
    ctx = portal_ctx()
    if not marker:
        (ctx.store_path / "format").unlink()
    (ctx.store_path / "decisions.db").write_text("plain text\n")
    with portal_client(ctx, raise_server_exceptions=False) as client:
        response = client.get("/api/session")

    _error(response, "refused" if marker else "not_found")
    if marker:
        assert response.json()["error"]["details"]["reason"] == "store_format"


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks require Windows developer mode")
def test_session_reports_a_symlinked_store_layout(
    portal_ctx, portal_client, tmp_path: Path
) -> None:
    ctx = portal_ctx()
    target = tmp_path / "elsewhere"
    (ctx.store_path / "entities").rename(target)
    (ctx.store_path / "entities").symlink_to(target, target_is_directory=True)
    with portal_client(ctx, raise_server_exceptions=False) as client:
        response = client.get("/api/session")
        _error(response, "refused")
        assert response.json()["error"]["details"]["reason"] == "store_format"
