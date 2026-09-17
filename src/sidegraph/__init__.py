"""Sidegraph — own the memory, rent the graph.

A decision / lessons layer: an append-only, repo-committed decision store layered as a
sidecar over a code-graph engine. See CLAUDE.md and ``docs/`` (user docs; the data
model lives in ``docs/concepts/data-model.md``).
"""

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

__version__ = "0.1.0"

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
