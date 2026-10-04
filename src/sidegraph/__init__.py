"""Sidegraph — own the memory, rent the graph.

A decision / lessons layer: an append-only, repo-committed decision store layered as a
sidecar over a code-graph engine. See CLAUDE.md and ``docs/`` (user docs; the data
model lives in ``docs/concepts/data-model.md``).

The re-exported names resolve on first access (PEP 562 ``__getattr__``), not at import:
importing any submodule runs this file first, and the hook entry points would otherwise pay
for pydantic, ulid and the models on every tool call just to reach ``sidegraph.config``.
see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D1)
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .schema import (
        SCHEMA_VERSION,
        AnchorBinding,
        Decision,
        DecisionKind,
        DecisionStatus,
        Domain,
        DomainStatus,
        Entity,
        Initiative,
        Provenance,
    )
    from .store import Store

__version__ = "0.9.0"

# name -> the submodule that defines it
_LAZY = {
    "SCHEMA_VERSION": "schema",
    "AnchorBinding": "schema",
    "Decision": "schema",
    "DecisionKind": "schema",
    "DecisionStatus": "schema",
    "Domain": "schema",
    "DomainStatus": "schema",
    "Entity": "schema",
    "Initiative": "schema",
    "Provenance": "schema",
    "Store": "store",
}

__all__ = [
    "SCHEMA_VERSION",
    "AnchorBinding",
    "Decision",
    "DecisionKind",
    "DecisionStatus",
    "Domain",
    "DomainStatus",
    "Entity",
    "Initiative",
    "Provenance",
    "Store",
    "__version__",
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
