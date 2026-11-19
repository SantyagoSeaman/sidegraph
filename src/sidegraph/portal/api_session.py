"""Session metadata, preferences and live ticket endpoints (D1, D3, D12–D13)."""

from datetime import timedelta
from typing import cast

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sidegraph.config import repository_root
from sidegraph.gitio import _run_git, store_change_count
from sidegraph.store_layout import VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY
from sidegraph.verify import find_store_project_repo

from . import contract, live, settings
from .context import PortalContext


def routes(ctx: PortalContext) -> list[Route]:
    """Register session information, preference updates and ticket minting (D3, D12–D13)."""

    async def ticket(request: Request) -> JSONResponse:
        return JSONResponse(live.mint_ticket(request).model_dump(mode="json"))

    def session(_request: Request) -> JSONResponse:
        with ctx.request_store() as store:
            store.refresh_if_stale()
            index_cold = store.get_meta(VOLATILE_STALE_KEY) == "1"
            last_stamp = store.get_meta(LAST_SYNCED_KEY)
        reader = ctx.reader()
        stamp = reader.sync_stamp() if reader is not None else None
        root = (
            ctx.store_path
            if (ctx.store_path / ".git").exists()
            else repository_root(ctx.store_path)
            or find_store_project_repo(ctx.store_path, timeout=1.0)
        )
        branch = None
        if root is not None:
            result = _run_git(["branch", "--show-current"], root, keep_repository_env=False)
            if result is not None and result.returncode == 0:
                branch = result.stdout.strip() or None
        family = ctx.host_family
        value = contract.Session(
            mode=ctx.mode,
            host_family=cast(contract.HostFamily, family)
            if family in ("claude", "codex", "grok", "muse")
            else None,
            settings=ctx.settings,
            repo_name=root.name if root is not None else None,
            branch=branch,
            index_cold=index_cold,
            graph_newer_than_sync=stamp is not None
            and last_stamp not in (stamp, f"index-only:{stamp}"),
            store_changes=store_change_count(ctx.store_path),
            started_at=ctx.started_at,
            reviewing_until=ctx.started_at + timedelta(minutes=10) if ctx.mode == "chat" else None,
        )
        return JSONResponse(value.model_dump(mode="json"))

    async def update(request: Request) -> JSONResponse:
        from .app import _error

        try:
            body = await request.json() if await request.body() else {}
        except ValueError:
            return _error("invalid", "Expected a JSON object.")
        if not isinstance(body, dict):
            return _error("invalid", "Expected a JSON object.")
        if "theme" not in body:
            return JSONResponse(ctx.settings.model_dump(mode="json"))
        theme = body["theme"]
        if theme not in ("dark", "light"):
            return _error("invalid", "Theme must be dark or light.", field="theme")
        try:
            value = settings.save(theme=theme)
        except settings.SettingsUnreadableError as exc:
            return _error("refused", str(exc), reason="settings_unreadable")
        except OSError:
            return _error(
                "refused", "Could not save portal settings.", reason="settings_unwritable"
            )
        ctx.settings.theme = value.theme
        return JSONResponse(ctx.settings.model_dump(mode="json"))

    handlers = {
        "/api/live-ticket": ticket,
        "/api/session": session,
        "/api/settings": update,
    }
    return [
        Route(route.path, handlers[route.path], methods=[route.method])
        for route in contract.ROUTES
        if route.module == "api_session"
    ]
