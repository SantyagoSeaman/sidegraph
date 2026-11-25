"""Portal routes and the wheel's direct dependencies and resources."""

import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from sidegraph.portal import contract

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("name", "floor"),
    [("starlette", "1.10"), ("uvicorn", "0.54"), ("sse-starlette", "3.7")],
)
def test_direct_portal_dependencies(name: str, floor: str) -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = {req.name: req for req in map(Requirement, project["dependencies"])}
    specifier = requirements[name].specifier
    pins = [spec.version for spec in specifier if spec.operator == "==" and "*" not in spec.version]
    if pins:
        # The released tree pins the whole closure exactly: the pin must sit at or above the floor.
        assert specifier.contains(pins[0])
        assert Version(pins[0]) >= Version(floor)
    else:
        assert specifier.contains(floor)
        assert not specifier.contains(f"{floor}.dev0", prereleases=True)
    assert project["scripts"]["sidegraph-portal"] == "sidegraph.portal.__main__:main"


def _pending(ctx) -> set[str]:
    from sidegraph.portal import api_agent, api_read, api_write

    return {
        module.__name__.rsplit(".", 1)[-1]
        for module in (api_agent, api_read, api_write)
        if not module.routes(ctx)
    }


def test_app_routes_match_contract(portal_ctx) -> None:
    from sidegraph.portal.app import create_app

    ctx = portal_ctx()
    pending = _pending(ctx)
    actual = [
        (method, route.path.replace("{path:path}", "{path}"))
        for route in create_app(ctx).routes
        for method in route.methods
    ]
    expected = [
        (route.method, route.path) for route in contract.ROUTES if route.module not in pending
    ]
    assert sorted(actual) == sorted(expected)


def test_no_route_pending(portal_ctx) -> None:
    assert not _pending(portal_ctx())


def test_vis_network_served(portal_ctx, portal_client) -> None:
    expected = files("sidegraph.viz").joinpath("assets", "vis-network.min.js").read_bytes()
    with portal_client(portal_ctx()) as client:
        response = client.get(contract.VIS_NETWORK_PATH)
        assert response.status_code == 200
        assert response.content == expected


@pytest.mark.slow
def test_wheel_contains_static(tmp_path: Path) -> None:
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=_ROOT, check=True)
    (wheel,) = tmp_path.glob("*.whl")
    with ZipFile(wheel) as archive:
        assert "sidegraph/portal/static/index.html" in archive.namelist()
