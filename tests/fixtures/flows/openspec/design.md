## Context

The importer currently drops archived-corpus context when a change folder moves.

## Goals / Non-Goals

Goal: keep decisions stable across the archive move. Non-goal: rewriting historical refs.

## Decisions

### Normalize the ref before parsing

**Rationale:** normalizing once at the point `rel_path` enters record construction keeps
the context stamp and the ref in agreement, so `_content_matches` can still recognize an
unedited doc across the move.

### Keep the on-disk path for anchor lookups

The graph's `source_file` never moves just because a change is archived, so anchor
resolution keeps using the real path.

## Risks / Trade-offs

A team that reuses a change name after archiving an unrelated same-named change collapses
both onto one ref; accepted, since it never happens in practice.
