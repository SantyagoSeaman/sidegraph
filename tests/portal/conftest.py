"""Isolated configuration and factories for the portal's HTTP tests."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from sidegraph.store import Store

if TYPE_CHECKING:
    from sidegraph.portal.context import PortalContext


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def portal_ctx(tmp_path: Path) -> Callable[..., "PortalContext"]:
    """Build a context with an existing isolated store and optional overrides."""
    from sidegraph.portal.context import PortalContext
    from sidegraph.portal.settings import load

    def make(**overrides: object) -> PortalContext:
        values = dict(
            store_path=tmp_path / ".sidegraph",
            graph_path=None,
            borrowed_from=None,
            host_family="claude",
            mode="cli",
            token="test-token",
            port=54321,
            started_at=datetime.now(UTC),
            settings=load(),
        )
        values.update(overrides)
        with Store(values["store_path"]):
            pass
        return PortalContext(**values)

    return make


@pytest.fixture
def portal_client() -> Callable[..., TestClient]:
    """Build a client with the portal's exact Host, Origin and token headers."""
    from sidegraph.portal.app import create_app

    def make(ctx: "PortalContext", **options: object) -> TestClient:
        origin = f"http://127.0.0.1:{ctx.port}"
        return TestClient(
            create_app(ctx),
            base_url=origin,
            headers={"Origin": origin, "X-Sidegraph-Token": ctx.token},
            **options,
        )

    return make
