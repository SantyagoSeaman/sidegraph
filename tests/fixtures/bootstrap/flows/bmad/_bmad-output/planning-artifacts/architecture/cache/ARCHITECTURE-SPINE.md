# Architecture Spine

## Design Paradigm
Local-first tooling with repository-owned state.

## Invariants & Rules

### AD-1 — Cache graph reads
Use one bounded in-process cache in `src/cache.py`.

## Deferred
A remote shared cache is deferred because it adds an outage domain.
