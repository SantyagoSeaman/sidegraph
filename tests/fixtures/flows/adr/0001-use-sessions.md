# Use server-side sessions for auth

## Context

We need to keep users logged in across requests. The `auth` module in `auth.py` handles login.

## Decision

Use server-side sessions stored in `sessions.py`, keyed by a signed cookie.

## Consequences

Sessions must be evicted on logout; horizontal scaling needs shared session storage.

## Alternatives considered

JWTs in localStorage — rejected: token revocation is hard and the XSS exposure is worse.
