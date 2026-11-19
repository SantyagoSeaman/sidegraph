"""Resources and launch identity for one Portal process (D1–D2, D11)."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sidegraph.config import borrowed_graph_path, default_graph_path
from sidegraph.engine.reader import GraphifyReader, ReaderCache
from sidegraph.store import Store
from sidegraph.store_layout import (
    LegacyStoreError,
    StoreRequirementError,
)

from .settings import PortalSettings

_READERS = ReaderCache()


def _same_graph_path(left: Path, right: Path) -> bool:
    # Directory aliases can point at the same graph and its adjacent reader sidecars.
    a = Path(os.path.abspath(left))
    b = Path(os.path.abspath(right))
    return a.name == b.name and a.parent.resolve() == b.parent.resolve()


@dataclass(frozen=True)
class PortalContext:
    """Resources shared across Portal handlers.

    See design/superpowers/specs/2026-11-17-portal-design.md (D1–D2, D11).
    """

    store_path: Path
    graph_path: Path | None
    borrowed_from: Path | None
    host_family: str
    mode: Literal["cli", "chat"]
    token: str
    port: int
    started_at: datetime
    settings: PortalSettings
    graph_overridden: bool = False

    def request_store(self) -> Store:
        """Open the existing store with the request busy timeout (D11)."""
        return self._open_store(2.0)

    def poller_store(self) -> Store:
        """Open a separate handle with the poller busy timeout (D11)."""
        return self._open_store(1.0)

    def _open_store(self, busy_timeout: float) -> Store:
        try:
            return Store(self.store_path, busy_timeout_seconds=busy_timeout, existing_only=True)
        except (LegacyStoreError, StoreRequirementError):
            raise
        except ValueError as exc:
            raise StoreRequirementError(str(exc), kind="malformed") from exc

    def reader(self) -> GraphifyReader | None:
        """Read the selected graph, if readable, through the shared reader factory (D2)."""
        if self.graph_path is None:
            return None
        try:
            return _READERS.get(self.graph_path)
        except Exception:
            return None

    @property
    def server_graph_differs(self) -> bool:
        """Whether the selected CLI graph differs from the MCP server's graph (D10)."""
        if self.mode != "cli" or not self.graph_overridden or self.graph_path is None:
            return False
        borrowed = borrowed_graph_path(self.store_path)
        server_graph = borrowed or default_graph_path(self.store_path)
        return not _same_graph_path(self.graph_path, server_graph) or (
            (borrowed is None) != (self.borrowed_from is None)
        )
