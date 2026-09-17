---
name: 'payments-platform'
type: architecture-spine
purpose: build-substrate
altitude: feature
paradigm: 'hexagonal'
scope: 'payment intake and retry pipeline'
status: final
created: '2026-07-28'
updated: '2026-07-30'
binds: []
sources: []
companions: []
---

# Architecture Spine — payments-platform

## Design Paradigm

Hexagonal: domain core in `payments/core`, adapters in `payments/adapters`. The intake
queue, the ledger client and the bank gateway are ports; nothing in core imports an
adapter.

## Invariants & Rules

### AD-1 — Bounded intake queue with producer backpressure

- **Binds:** payment intake, retry pipeline
- **Prevents:** unbounded queue growth OOM-killing the worker under retry storms
- **Rule:** `IntakeQueue` is capped; producers receive backpressure instead of buffering

### AD-2 — [ADOPTED] Idempotency keys on every mutation

- **Binds:** all
- **Prevents:** double-charging on retried requests
- **Rule:** every mutating endpoint requires an idempotency key via `IdempotencyStore`; replays return the original result

## Consistency Conventions

| Concern | Convention |
| --- | --- |
| Data & formats (ids, dates, error shapes, envelopes) | money as integer minor units; error envelope carries code, message, retryable |

## Stack

| Name | Version |
| --- | --- |
| Python | 3.13 |

## Structural Seed

```text
payments/
  core/      # domain logic, no IO
  adapters/  # queue, ledger, gateway port implementations
```

## Deferred

- Multi-currency settlement — postponed until a second currency actually onboards.
