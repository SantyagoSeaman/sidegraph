## Summary

A short teaser paragraph that isn't part of any known section.

## Context

The importer currently drops archived-corpus context when a change folder moves.

## Decisions

### Normalize the ref before parsing

Normalize once, at the point `rel_path` enters record construction.

### Keep the on-disk path for anchor lookups

Anchor resolution keeps using the real, on-disk path.
