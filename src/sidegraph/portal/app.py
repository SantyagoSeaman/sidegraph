"""The Portal ASGI app and its loopback security boundary (D2–D3)."""

import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from secrets import compare_digest
from time import monotonic

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from sidegraph.store import _STORE_MOVED_OR_MISSING, StoreBusyError
from sidegraph.store_layout import LegacyStoreError, StoreRequirementError

from . import api_agent, api_read, api_session, api_write, contract, live
from .context import PortalContext
from .opener import forget_redirect_file

_STATIC = Path(__file__).with_name("static")
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _error(code: str, message: str, **details: JsonValue) -> JSONResponse:
    body = contract.ErrorBody.model_validate(
        {"error": {"code": code, "message": message, "details": details}}
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=contract.ERROR_CODES[code])


def create_app(ctx: PortalContext) -> Starlette:
    """Compose the contract routes, security checks and packaged assets.

    See design/superpowers/specs/2026-11-17-portal-design.md (D2–D3, D11–D13).
    """
    host = f"127.0.0.1:{ctx.port}"
    origin = f"http://{host}"
    token = ctx.token.encode()
    live_path = next(route.path for route in contract.ROUTES if route.auth == "ticket")
    last_activity = monotonic()
    static = StaticFiles(directory=_STATIC)

    async def secure(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonlocal last_activity
        response: Response
        path = request.scope["path"]
        request_origins = request.headers.getlist("origin")
        if request.headers.getlist("host") != [host]:
            response = _error("bad_host", "Use the portal address printed at launch.")
        elif (
            request.method == "POST" or (path == live_path and request_origins)
        ) and request_origins != [origin]:
            response = _error("bad_origin", "This request must come from the portal page.")
        elif path.startswith("/api/") and not compare_digest(
            request.headers.get("X-Sidegraph-Token", "").encode("latin-1"), token
        ):
            response = _error("unauthorized", "The portal you opened has closed.")
        else:
            if path.startswith("/api/"):
                if not request.app.state.authenticated:
                    request.app.state.authenticated = True
                    forget_redirect_file()
                if request.headers.get("X-Sidegraph-Activity") == "user":
                    last_activity = monotonic()
            response = await call_next(request)
        response.headers.update(_HEADERS)
        return response

    def shell(_request: Request) -> HTMLResponse:
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(contract.THEME_MARKER, f"th-{ctx.settings.theme}"))

    async def asset(request: Request) -> Response:
        if request.scope["path"] == contract.VIS_NETWORK_PATH:
            match = files("sidegraph.viz").joinpath("assets", "vis-network.min.js")
            return Response(match.read_bytes(), media_type="text/javascript")
        response = await static.get_response(request.path_params["path"], request.scope)
        if isinstance(response, FileResponse) and Path(response.path).suffix.lower() in (
            ".js",
            ".mjs",
        ):
            # Windows registry MIME types can label JavaScript as plain text.
            response.headers["Content-Type"] = "text/javascript; charset=utf-8"
        return response

    def http_error(_request: Request, exc: Exception) -> Response:
        if isinstance(exc, FileNotFoundError):
            if exc.strerror == _STORE_MOVED_OR_MISSING:
                return _error("not_found", "The store moved or is missing.")
            return _error("not_found", "The requested resource is unavailable.")
        assert isinstance(exc, HTTPException)
        if exc.status_code == 405:
            response = _error("invalid", "This method is unavailable for the requested route.")
            response.status_code = 405
            response.headers.update(exc.headers or {})
            return response
        code = "unauthorized" if exc.status_code == 401 else "not_found"
        return _error(code, exc.detail)

    def store_error(_request: Request, exc: Exception) -> Response:
        error_code = getattr(exc, "sqlite_errorcode", 0)
        if isinstance(exc, StoreBusyError) or error_code & 0xFF in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):
            return _error("busy", "The store is busy. Try again shortly.")
        raise exc

    def store_refused(_request: Request, exc: Exception) -> Response:
        reason = "store_format" if isinstance(exc, StoreRequirementError) else "legacy_store"
        return _error("refused", str(exc), reason=reason)

    def internal_error(_request: Request, _exc: Exception) -> Response:
        return Response("Internal Server Error", status_code=500, headers=_HEADERS)

    public = {"/": shell, "/static/{path}": asset}
    routes = [
        Route(
            route.path.replace("{path}", "{path:path}"),
            public[route.path],
            methods=[route.method],
        )
        for route in contract.ROUTES
        if route.module == "static"
    ]
    for module in (api_session, live, api_read, api_write, api_agent):
        routes.extend(module.routes(ctx))
    for route in routes:
        if route.methods:
            route.methods.discard("HEAD")  # Starlette otherwise adds it implicitly to GET.

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            forget_redirect_file()

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=secure)],
        exception_handlers={
            HTTPException: http_error,
            FileNotFoundError: http_error,
            sqlite3.OperationalError: store_error,
            StoreRequirementError: store_refused,
            LegacyStoreError: store_refused,
            Exception: internal_error,
        },
    )
    app.router.redirect_slashes = False
    app.state.authenticated = False
    app.state.live_tickets = {}
    app.state.broadcaster = live.Broadcaster()
    app.state.last_user_activity = lambda: last_activity
    return app
