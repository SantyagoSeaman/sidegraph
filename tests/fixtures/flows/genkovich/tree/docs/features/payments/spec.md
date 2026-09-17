---
status: ACCEPTED
owner: payments
reviewers: platform
updated_at: 2026-07-20
feature_size: M
---
# Spec — payments-queue-backpressure

## 1. Context

Unbounded queue growth OOM-kills the payments worker under retry storms.

## 2. Goals

Bound worker memory without dropping payments.

## 3. Non-goals

Changing the ledger's write path.
