# Cache design

## Goal
Avoid repeated graph parsing.

## Architecture
Use one process-local immutable cache in `src/cache.py`.

## Alternatives
Do not add Redis because local state is sufficient.
