---
status: ACCEPTED
owner: payments
reviewers: platform
updated_at: 2026-07-20
feature_size: M
target_surfaces: worker, api
---
# Software Architecture Document — payments

## 1. Introduction and goals

Keep payment processing correct under downstream outages without unbounded resource growth.

## 3. Context and scope

The worker sits between the payments API and the ledger, and owns the retry queue.

## 4. Solution strategy

A bounded retry queue with producer backpressure, fronted by an idempotency key per payment.

## 9. Architecture decisions

ADR-0001 bounds the queue at 10k with backpressure.

## 11. Risks and technical debt

At-least-once delivery means consumers must be idempotent; the shed path needs monitoring.
