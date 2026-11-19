"""Launch the loopback Portal in CLI or chat mode (D1)."""

import argparse
import asyncio
import logging
import os
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from io import TextIOWrapper
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic
from typing import get_args

import uvicorn

from sidegraph.config import (
    borrowed_graph_path,
    default_graph_path,
    main_checkout_root,
    missing_store_message,
    resolve_store_location,
)
from sidegraph.store import _has_existing_store
from sidegraph.store_layout import check_store_requirement

from .app import create_app
from .context import PortalContext, _same_graph_path
from .contract import HostFamily
from .opener import forget_redirect_file, open_via_redirect_file
from .settings import load


def _watch_lifetime(fd: int, closed: threading.Event) -> None:
    if sys.platform == "win32":
        import msvcrt

        fd = msvcrt.open_osfhandle(fd, os.O_RDONLY | os.O_BINARY)
    try:
        os.set_inheritable(fd, False)
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while pipe.read(1):
                pass
    finally:
        closed.set()


def main(argv: list[str] | None = None, *, opener: Callable[[str], object] | None = None) -> int:
    """Run one Portal against an existing store.

    See design/superpowers/specs/2026-11-17-portal-design.md (D1–D3).
    """
    families = get_args(HostFamily.__value__)
    parser = argparse.ArgumentParser(description="Open the Sidegraph Portal for an existing store.")
    parser.add_argument("--db", help="Path to the store")
    parser.add_argument("--graph", type=Path, help="Use this graph in CLI mode")
    parser.add_argument("--port", type=int, default=0, help="Loopback port (0 selects a free port)")
    parser.add_argument(
        "--no-browser", action="store_true", help="Print the URL without opening it"
    )
    parser.add_argument(
        "--host-family",
        metavar="{" + ",".join(families) + "}",
        help="Host family for the Agent view",
    )
    parser.add_argument("--lifetime-fd", type=int, help="Inherited lifetime pipe (chat mode)")
    args = parser.parse_args(argv)
    chat = args.lifetime_fd is not None
    if args.host_family is not None and args.host_family not in families:
        if not chat:
            parser.error(f"argument --host-family: expected one of {', '.join(families)}")
        args.host_family = None
    if chat and args.graph is not None:
        parser.error("--graph is available in CLI mode only")
    if chat and isinstance(sys.stdout, TextIOWrapper):
        # The startup line is a UTF-8 protocol independent of host stdio settings.
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    if args.port == 80:
        print("error Port 80 cannot be used by the portal.", flush=True)
        return 1

    location = resolve_store_location(args.db, search_ancestors=True, warn_on_create=False)
    # Chat mode uses MCP's path spelling so symlinks followed by '..' work for
    # store access and the shared graph lookup sees the same input as MCP.
    store = Path(location.path).absolute() if chat else Path(os.path.abspath(location.path))
    try:
        if store.is_file():
            print("error Pass the store directory to --db.", flush=True)
            return 1
        existing = _has_existing_store(store)
        if existing:
            check_store_requirement(store)
    except (OSError, ValueError) as exc:
        print(f"error Cannot inspect the store: {exc}", flush=True)
        return 1
    if not existing:
        print(f"error {missing_store_message(store)}", flush=True)
        return 1

    borrowed = borrowed_graph_path(store)
    graph = (
        Path(os.path.abspath(args.graph))
        if args.graph is not None
        else borrowed or default_graph_path(store)
    )
    borrowed_from = (
        main_checkout_root(store)
        if borrowed is not None and _same_graph_path(graph, borrowed)
        else None
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
            listener.listen()
        except (OSError, OverflowError) as exc:
            print(f"error Cannot bind the portal port: {exc}", flush=True)
            return 1

        ctx = PortalContext(
            store_path=store,
            graph_path=graph,
            borrowed_from=borrowed_from,
            host_family=args.host_family or ("" if chat else "claude"),
            mode="chat" if chat else "cli",
            token=token_urlsafe(32),
            port=listener.getsockname()[1],
            started_at=datetime.now(UTC),
            settings=load(),
            graph_overridden=args.graph is not None,
        )
        app = create_app(ctx)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=ctx.port,
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=1,
            )
        )
        url = f"http://127.0.0.1:{ctx.port}/#t={ctx.token}"
        print(f"url {url}", flush=True)
        if chat:
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), 1)
        closed = threading.Event()
        if args.lifetime_fd is not None:
            threading.Thread(
                target=_watch_lifetime, args=(args.lifetime_fd, closed), daemon=True
            ).start()

        def open_browser(path: str) -> None:
            if server.should_exit or closed.is_set() or app.state.authenticated:
                forget_redirect_file()
            elif opener is None:
                webbrowser.open(Path(path).as_uri())
            else:
                opener(path)

        def open_redirect() -> None:
            try:
                open_via_redirect_file(url, open_browser)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Could not open the browser. Use the URL printed at launch."
                )

        if not args.no_browser:
            threading.Thread(target=open_redirect, daemon=True).start()

        async def monitor() -> None:
            while True:
                idle = monotonic() - app.state.last_user_activity()
                if server.should_exit or closed.is_set() or idle >= ctx.settings.idle_minutes * 60:
                    app.state.broadcaster.publish("closing", {})
                    server.should_exit = True
                    return
                await asyncio.sleep(0.1)

        async def serve() -> None:
            watcher = asyncio.create_task(monitor())
            try:
                await server.serve(sockets=[listener])
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            return 0
        finally:
            server.should_exit = True
            forget_redirect_file()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
