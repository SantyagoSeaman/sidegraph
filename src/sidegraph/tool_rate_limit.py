"""Bound tool admission with one process-local token bucket; never queue calls."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult


class ToolCallRateLimit(Middleware):
    """Allow a generous local burst while bounding accidental invocation loops.

    The bucket belongs to this server process, not a client-supplied identity. Ping,
    initialization and discovery bypass it. Refusals happen before tool work begins.
    """

    def __init__(
        self,
        *,
        rate: float = 100,
        capacity: int = 200,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(rate) or rate <= 0 or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("Tool admission rate and capacity must be finite and positive.")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()
        self._lock = threading.Lock()

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> ToolResult:
        """Reserve a token under a thread lock, then dispatch outside the lock."""
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._updated)
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = max(self._updated, now)
            admitted = self._tokens >= 1
            if admitted:
                self._tokens -= 1
        if not admitted:
            raise ToolError("Tool call rate exceeded; wait briefly and retry.")
        return await call_next(context)
