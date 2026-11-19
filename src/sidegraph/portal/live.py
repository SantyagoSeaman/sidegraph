"""Ticket-authenticated server-sent events (D3, D11)."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import suppress
from secrets import token_urlsafe
from time import monotonic

from pydantic import TypeAdapter
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from sidegraph.store import Store
from sidegraph.store_layout import REBIND_DONE_KEY, VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY

from . import contract
from .context import PortalContext

_EVENTS = TypeAdapter(contract.LiveEvent)


class Broadcaster:
    """Fan out Portal events to the streams open on the app's event loop (D11)."""

    def __init__(self) -> None:
        self._streams: set[asyncio.Queue[ServerSentEvent]] = set()
        self._closing = False

    def publish(self, event: str, data: dict) -> None:
        """Send a contract event to every subscriber (D11).

        Call on the app's event loop. A worker thread can schedule this call with
        ``loop.call_soon_threadsafe``.
        """
        message = _EVENTS.validate_python(
            {"event": event, "data": data if event == "changed" else ""}
        )
        payload = (
            json.dumps(message.data, separators=(",", ":")) if message.event == "changed" else ""
        )
        frame = ServerSentEvent(event=message.event, data=payload)
        if message.event == "closing":
            self._closing = True
        for stream in self._streams:
            stream.put_nowait(frame)

    async def stream(self, poller: "Poller | None" = None) -> AsyncIterator[ServerSentEvent]:
        """Subscribe until the client disconnects or the Portal closes (D11)."""
        queue: asyncio.Queue[ServerSentEvent] = asyncio.Queue()
        self._streams.add(queue)
        try:
            if poller is not None:
                if not self._closing:
                    await poller.start()
                yield ServerSentEvent(comment="")  # Consumed in the route before sending headers.
            if self._closing:
                yield ServerSentEvent(event="closing", data="")
                return
            while True:
                frame = await queue.get()
                yield frame
                if frame.event == "closing":
                    break
        finally:
            self._streams.remove(queue)
            if poller is not None and not self._streams:
                poller.stop()


class Poller:
    """Detect external store and graph changes while streams are open (D9, D11)."""

    def __init__(self, ctx: PortalContext, broadcaster: Broadcaster) -> None:
        self._ctx = ctx
        self._broadcaster = broadcaster
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._sampled = asyncio.Event()

    async def start(self) -> None:
        """Start polling and wait for the first snapshot attempt (D11)."""
        self._stopped.clear()
        if self._task is None:
            self._sampled.clear()
            self._task = asyncio.create_task(self._run())
        await self._sampled.wait()

    def stop(self) -> None:
        """Wake the poller after the last stream leaves and let any read finish (D11)."""
        self._stopped.set()

    def _snapshot(self, store: Store) -> dict[contract.Change, object]:
        graph = None
        if self._ctx.graph_path is not None:
            try:
                st = self._ctx.graph_path.stat()
            except FileNotFoundError:
                pass
            else:
                # Compare the same identity as ReaderCache, without parsing the graph.
                graph = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        store.refresh_if_stale()
        return {
            "records": store.canonical_digest(),
            "graph": graph,
            "sync": (
                store.get_meta(VOLATILE_STALE_KEY),
                store.get_meta(REBIND_DONE_KEY),
                store.get_meta(LAST_SYNCED_KEY),
                store.binding_fingerprint(),
            ),
            "advice": store.advice_fingerprint(),
        }

    async def _run(self) -> None:
        previous: dict[contract.Change, object] | None = None
        store: Store | None = None
        loop = asyncio.get_running_loop()
        interval = self._ctx.settings.poll_seconds
        deadline = loop.time()
        try:
            while not self._stopped.is_set():
                try:
                    if store is None:
                        store = await asyncio.to_thread(self._ctx.poller_store)
                        if self._stopped.is_set():
                            break
                    current = await asyncio.to_thread(self._snapshot, store)
                except (sqlite3.DatabaseError, OSError, ValueError) as exc:
                    if store is not None and isinstance(exc, sqlite3.DatabaseError):
                        await asyncio.to_thread(store.close)
                        store = None
                    if not self._stopped.is_set():
                        self._broadcaster.publish("stale", {})
                else:
                    # If the initial attempt failed, refetch what clients may have read while
                    # stale. There is no last good state to compare in that case.
                    if self._sampled.is_set() and not self._stopped.is_set():
                        for what in current:
                            if previous is None or current[what] != previous[what]:
                                self._broadcaster.publish("changed", {"what": what})
                    previous = current
                self._sampled.set()
                # Skip missed ticks on the original cadence. A stop/start pair can wake
                # the waiter early, so keep waiting for the same tick if still subscribed.
                deadline += ((loop.time() - deadline) // interval + 1) * interval
                while not self._stopped.is_set() and (remaining := deadline - loop.time()) > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stopped.wait(), remaining)
        finally:
            self._sampled.set()
            self._task = None
            if store is not None:
                await asyncio.to_thread(store.close)


def mint_ticket(request: Request) -> contract.LiveTicket:
    """Mint a single-use, 30-second stream ticket for the authenticated endpoint (D3)."""
    tickets: dict[str, float] = request.app.state.live_tickets
    now = monotonic()
    for ticket, expiration in list(tickets.items()):
        if expiration <= now:
            del tickets[ticket]
    ticket = token_urlsafe(32)
    tickets[ticket] = now + 30.0
    return contract.LiveTicket(ticket=ticket)


def routes(ctx: PortalContext) -> list[Route]:
    """Serve the ticket-authenticated stream (D3, D11)."""
    poller: Poller | None = None

    async def live(request: Request) -> Response:
        nonlocal poller
        tickets: dict[str, float] = request.app.state.live_tickets
        candidates = request.query_params.getlist("ticket")
        expiration = tickets.pop(candidates[0], None) if len(candidates) == 1 else None
        if expiration is None or expiration <= monotonic():
            raise HTTPException(
                contract.ERROR_CODES["unauthorized"],
                "This stream ticket is missing, expired or already used.",
            )
        broadcaster: Broadcaster = request.app.state.broadcaster
        if poller is None:
            poller = Poller(ctx, broadcaster)
        subscription = broadcaster.stream(poller)
        # The browser refetches on open. Take the baseline before that refetch can race it.
        await anext(subscription)
        return EventSourceResponse(subscription, shutdown_grace_period=0.5)

    return [
        Route(route.path, live, methods=[route.method])
        for route in contract.ROUTES
        if route.module == "live"
    ]
