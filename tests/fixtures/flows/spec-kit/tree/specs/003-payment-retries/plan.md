# Implementation Plan: payment retries

**Branch**: `003-payment-retries` **Date**: 2026-07-27 **Spec**: specs/003-payment-retries/spec.md

## Summary

Replace the fixed 3-attempt retry with exponential backoff and jitter so consumers stop
retrying in lockstep during a downstream outage.

## Technical Context

Retries currently happen in `worker.py` with a fixed 3-attempt bound and no jitter, so a
downstream outage produces a synchronized retry wave across the fleet.

## Constitution Check

Two principles are in tension: "no unbounded work" and "no dropped payments".

## Project Structure

Retry logic stays in `worker.py`; the backoff schedule moves to a new `backoff.py` module.

## Complexity Tracking

Unbounded retry with a dead-letter queue was rejected: it satisfies "no dropped payments" but
violates "no unbounded work", and the DLQ became a second unmonitored queue in the last
incident. Fixed-bound-with-jitter was chosen instead.
