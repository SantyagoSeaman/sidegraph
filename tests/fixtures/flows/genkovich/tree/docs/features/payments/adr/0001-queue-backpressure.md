---
status: ACCEPTED
owner: payments
reviewers: platform
updated_at: 2026-07-20
feature_size: M
ticket: PAY-412
---
# 0001 — Bound the payment queue with backpressure

## Context

The payments worker drains an unbounded queue in `worker.py`. Under a retry storm the queue grows
until the pod is OOM-killed, and the restart replays the same storm.

## Decision drivers

Memory ceiling must be bounded. No payment may be silently dropped.

## Considered options

Autoscaling the worker pool: it scales the consumer but not the memory ceiling, so the same OOM
happens later with a larger bill. Dropping oldest-first: silently loses payments, which is
unacceptable for this queue specifically.

## Decision outcome

Bound the queue at 10k entries and apply backpressure to the producer, shedding at the edge rather
than in the worker.

## Consequences

### Negative

Producers must handle a rejection path.

### Neutral

The shed signal becomes a monitored metric.

## Links

- PAY-412
