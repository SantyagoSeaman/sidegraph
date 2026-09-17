---
name: manage-domains
description: Use for managing, editing, fixing, splitting, or dropping domains after the initial naming pass — "this domain is wrong", "split payments into two", "add a domain for X", "rename that domain", "drop this domain", "show me all domains". Also reachable directly as /sidegraph:manage-domains. The escape hatch and ongoing-maintenance flow for individual domains, by key — the a-la-carte counterpart to name-domains' whole-set picker.
---

# Manage domains

The escape hatch and ongoing-maintenance flow: add, rename, drop, or merge **individual**
domains by key, at the human's direction. Builds on the same tools `name-domains` uses
(`list_domain_candidates`, `propose_domains`/`add_domain`, `ratify`) — used one domain at a
time instead of as a batch pick — plus two tools `name-domains` has no need for:
`list_domains` (full listing, any status) and `supersede_domain` (rename/re-scope).

**Same gate as everywhere else in Sidegraph: `propose_domains`/`add_domain` land
`status=proposed`; nothing is live/TOC-visible until `ratify` accepts it** (`propose_domains`
under `SIDEGRAPH_RATIFY_POLICY=auto-all` may accept an eligible draft at write time — check
its `ratified_by`). Don't skip straight
to `ratify` on a guess — confirm the specific change with the human first when it's ambiguous
(which domain, which communities), the same way `name-domains` waits for an explicit pick.

## "Show me all domains"

Call `list_domains()` — the full listing tool, every domain in the store regardless of status.
Each entry: `{id, slug, title, summary, status, member_count, path_prefixes,
seed_anchor_count, parent_slug, child_slugs}` — `member_count` is `len(communities)` (0 for a
domain not yet ratified/synced), `parent_slug`/`child_slugs` give you the hierarchy without a
separate lookup. Pass `status="accepted"` (or `"proposed"`/`"dropped"`/`"superseded"`) to
narrow. This is the tool for "show me all domains" — prefer it over the narrower views below:

- **`list_proposed()`** — domains awaiting ratification only (`status=proposed`), rendered
  human-readably with their `paths:`/`communities:` membership rule — reach for this
  specifically at ratify time, not as a general listing.
- **`drill_down(domain_slug)`** — full detail (summary, subdomains, members, decisions) for one
  **known** slug — the natural follow-up once `list_domains` has given you the slug you want
  to go deeper on. Passing an unknown slug returns up to 10 currently-accepted slugs to retry
  with instead of guessing.
- **`list_domain_candidates()`** — the *unclaimed* side only (communities with no domain yet);
  `already_claimed` gives you a count of already-named communities but not their slugs (use
  `list_domains` for that).

## Add a domain by anchoring its members

1. Find the community/anchor for the target code area: `list_domain_candidates()` (optionally
   narrowed with `paths=[...]`) — read `top_file`/`top_members` for any candidate you're not
   sure about, same discipline as `name-domains` step 2.
2. Take each chosen candidate's own `anchor` field (`{"name", "file_path"}` — **not** its
   `community` id, which is volatile and doesn't survive a fresh clone or graph rebuild) and
   put it in `seed_anchors`: `add_domain(slug=..., title=..., summary=..., seed_anchors=[{
   "name": "OrderBook", "file_path": "trader/order_book.py"}, ...])` — or
   `propose_domains(drafts=[{"slug": ..., "title": ..., "summary": ..., "seed_anchors":
   [...]}])` for a session-drafted proposal. Use `path_prefixes` instead of/alongside
   `seed_anchors` when the domain has one clean shared path.
3. `ratify(accept=[<domain_id>])` to make it live — this also resolves `seed_anchors`/
   `path_prefixes` into `communities` immediately, so `drill_down` shows real membership
   without a separate sync — or leave it `proposed` for a human to accept later via
   `sidegraph-ratify`/`list_proposed`.

## Rename a domain

Use the `supersede_domain` MCP tool — it wraps `Store.supersede_domain` directly, so this is
no longer a "drop the raw `Store` API script" situation:

```python
supersede_domain(
    old_slug_or_id="<old slug or domain_id>",
    new_slug="<new slug>",
    new_title="<new title>",
    new_summary="<new summary>",
    path_prefixes=[...],   # optional -- the successor's OWN membership rule, from scratch
    seed_anchors=[...],    # optional -- durable entity anchors, from scratch (see "Add a domain" above)
    parent_slug=...,       # optional
)
```

It closes the old domain (`status -> superseded`: retrievable history, no longer live/
TOC-visible) and writes the successor in the same call — same one-gate rule as everywhere
else: the successor lands `status=proposed`, so `ratify(accept=[<new_domain_id>])` is still
needed to make the rename live.

**Nothing is inherited.** The successor's `path_prefixes`/`seed_anchors` start at `[]` unless
you pass them explicitly — to keep covering the same code, carry over whatever membership rule
the old domain had. `drill_down` won't help here (it shows rendered member lines, not the raw
rule), but you don't need git-history archaeology either:

- **`path_prefixes`** reads back out as structured data — `list_domains()` returns it verbatim
  per domain (`{"path_prefixes": [...], ...}`), no file read needed.
- **`seed_anchors`** has no structured tool-read for the descriptors themselves —
  `list_domains()` gives you only `seed_anchor_count` (how many, not what). For the actual
  `{"name", "file_path"}` list, read the committed domain file directly:
  `.sidegraph/domains/<domain_id>.json` (the full row minus the volatile `communities` field —
  see [store format](../../../../docs/reference/store-format.md)) — still structured JSON, just
  not exposed through an MCP tool yet.

`supersede_domain` is also the mechanism behind the naming guide's [recovering from a
mass-drop](../../../../docs/guides/naming-your-domains.md#recovering-from-a-mass-drop): give
the successor NO matching `path_prefixes`/`seed_anchors` (instead of carrying the old rule
forward) and its `communities` stays empty, freeing the community for a future
`sidegraph-domains bootstrap` pass — the opposite intent from an ordinary rename, so be
deliberate about which one you're doing.

## Drop a wrong domain

`ratify(drop=[<id>])` — works on **both** `proposed` and `accepted` domains (unlike a decision
drop, which is proposal-only). This is also the resolution verb for a slug conflict: if two
branches each independently accepted a domain at the same slug, drop the one that loses.
Dropping is append-only (the record stays, status flips to `dropped`, fully retrievable) —
never a hard delete.

## Split a domain into two

There's no single-call split. Treat it as: add two new domains, each anchoring the subset of
members that belongs to it via `seed_anchors`/`path_prefixes` (see "Add a domain" above — use
`list_domain_candidates(paths=[...])` or your own knowledge of the code to divide the
membership), ratify both, then drop the original (see "Drop a wrong domain" above).

## See also

- `sidegraph:name-domains` — the whole-set onboarding flow; use it for the *first* naming pass
  on a repo, not one-off edits.
- [`docs/guides/naming-your-domains.md`](../../../../docs/guides/naming-your-domains.md) —
  background on bootstrap idempotency, mass-drop recovery, and the CLI equivalents
  (`sidegraph-domains add`, `sidegraph-ratify`).
