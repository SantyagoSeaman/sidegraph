"""The browser receives a private redirect file and the Portal cleans it up."""

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from sidegraph.portal import contract


def test_redirect_path_is_private_and_deleted_at_exit(tmp_path: Path, monkeypatch) -> None:
    from sidegraph.portal import opener

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    url = "http://127.0.0.1:54321/#t=private-test-token"
    args: list[str] = []
    try:
        redirect = opener.open_via_redirect_file(url, args.append)
        assert args == [str(redirect)]
        if os.name != "nt":
            assert stat.S_IMODE(redirect.stat().st_mode) == 0o600
        assert f"location.replace({json.dumps(url)})" in redirect.read_text(encoding="utf-8")
    finally:
        opener.forget_redirect_file()
    assert not redirect.exists()


def test_redirect_deleted_on_first_authenticated_request(portal_ctx, portal_client) -> None:
    from sidegraph.portal import opener

    ctx = portal_ctx()
    redirect = opener.open_via_redirect_file(
        f"http://127.0.0.1:{ctx.port}/#t={ctx.token}", lambda _path: None
    )
    try:
        with portal_client(ctx) as client:
            assert client.get("/").status_code == 200
            assert redirect.exists()
            client.headers["X-Sidegraph-Token"] = "wrong"
            assert client.post("/api/live-ticket").status_code == 401
            assert redirect.exists()
            client.headers["X-Sidegraph-Token"] = ctx.token
            assert client.post("/api/live-ticket").status_code == 200
            assert not redirect.exists()
    finally:
        opener.forget_redirect_file()


def test_main_opens_the_redirect_in_a_thread(
    portal_ctx,
    tmp_path: Path,
    capsys,
) -> None:
    from sidegraph.portal.__main__ import main

    config = Path(os.environ["XDG_CONFIG_HOME"]) / "sidegraph" / "portal.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"idle_minutes": 0.05}', encoding="utf-8")
    ctx = portal_ctx()
    received: list[tuple[Path, str, int]] = []

    def launch(path: str) -> None:
        redirect = Path(path)
        match = re.search(r"location\.replace\((.+)\)", redirect.read_text(encoding="utf-8"))
        assert match is not None
        url = json.loads(match[1])
        parsed = urlsplit(url)
        response = httpx.get(
            f"{parsed.scheme}://{parsed.netloc}/api/session",
            headers={"X-Sidegraph-Token": parsed.fragment.removeprefix("t=")},
            timeout=2,
            trust_env=False,
        )
        contract.Session.model_validate(response.json())
        received.append((redirect, url, response.status_code))

    assert main(["--db", str(ctx.store_path)], opener=launch) == 0
    assert len(received) == 1
    redirect, url, status = received[0]
    assert capsys.readouterr().out == f"url {url}\n"
    assert status == 200
    assert not redirect.exists()
