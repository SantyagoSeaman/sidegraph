## Why

Users want a way to reject a proposed change with a documented reason.

## What Changes

Add a `--reject` flag that records the reason in the change's proposal.md.

## Alternatives

Keeping rejection reasons only in commit messages, dropped because they aren't
discoverable from the change folder itself.
