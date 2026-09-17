# Use bounded retries

## Context
Requests can fail transiently.

## Decision
Use three bounded retries in `src/client.py`.

## Rejected
Do not retry forever because it hides outages.
