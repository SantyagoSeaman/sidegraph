# Implementation Plan: Cache

## Summary
Implement a bounded in-process cache in `src/cache.py`.

## Technical Context
The data is repository-local and immutable during one command.

## Complexity Tracking
A remote service was rejected because it adds operational cost.
