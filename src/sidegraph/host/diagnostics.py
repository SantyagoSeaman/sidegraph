"""One bounded, content-free stderr signal per synchronous hook invocation."""

from __future__ import annotations

import json
import os
import sys


def _identity() -> tuple[int, int, int]:
    stat = os.fstat(2)
    return stat.st_dev, stat.st_ino, stat.st_mode


def _field(value: str, limit: int) -> str:
    # Bound work before sanitizing; JSON escaping cannot expand the ASCII allowlist.
    return "".join(c if c.isascii() and (c.isalnum() or c in "_.-") else "_" for c in value[:limit])


class HookDiagnostics:
    """Best-effort reporting, independent of the store and of buffered stream cleanup.

    Never adopt fd2 opened after startup: it could be a store or index file. No descriptor
    is duplicated, closed or owned here. This guard is for standalone single-threaded hooks.
    """

    def __init__(self, hook: str) -> None:
        self._hook = hook
        self._attempted = False
        self._stderr: tuple[int, int, int] | None = None
        try:
            if sys.stderr is not None and not sys.stderr.closed:
                self._stderr = _identity()
        except Exception:
            pass

    def report(self, stage: str, error: Exception) -> None:
        """Attempt one <=256-byte ASCII line; never format the error's private message."""
        if self._attempted:
            return
        self._attempted = True
        try:
            if self._stderr is None:
                return
            line = (
                json.dumps(
                    {
                        "sidegraph_hook_error": True,
                        "hook": _field(self._hook, 24),
                        "stage": _field(stage, 32),
                        "exception": _field(type(error).__name__, 64),
                    },
                    separators=(",", ":"),
                ).encode("ascii")
                + b"\n"
            )
            if len(line) <= 256 and _identity() == self._stderr:
                os.write(2, line)
        except Exception:
            pass
