---
status: draft
---
# Cache the entitlements lookup

## Goal

Cut the entitlements lookup latency on the hot path in `entitlements.py`.

## Architecture

Add a 60-second in-process TTL cache keyed by `UserId`.
