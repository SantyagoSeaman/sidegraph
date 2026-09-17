---
status: draft
---
# Retry policy for the payments worker

## Goal

Stop double-charging customers when the payments worker retries after a timeout.

## Architecture

Add an idempotency key to `payments.py`, stored per `OrderId`, checked before submit.

## Approaches considered

Distributed locks — rejected: too heavy and adds a new failure mode.
At-least-once with dedup at the gateway — rejected: the gateway cannot see the OrderId.

## Risks

If the idempotency store is unavailable, submits fall back to at-least-once — a known gap.
