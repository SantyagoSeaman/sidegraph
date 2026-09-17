---
name: name-domains
description: Use when the user wants domains named, organized, or onboarded for the first time — "name my domains", "set up domains", "give this project a table of contents", "make a mind map of this codebase", "organize the codebase into named areas". Also reachable directly as /sidegraph:name-domains. Turns the graph's raw Leiden communities into 2-3 human-picked, named domain sets via the list_domain_candidates MCP tool — the primary onboarding path (CLI bootstrap is the scripted/CI alternative).
---

# Name domains

Turn the graph's nameless community structure into a small set of named, described domains —
the table of contents `SessionStart`/`drill_down` serve from. You are the curator; the core
MCP tools (`list_domain_candidates`, `propose_domains`, `ratify`) are read-only or
deterministic and never decide names — you do, from the actual code.

**HARD GATE — never call `propose_domains` or `ratify` before the human has explicitly picked
one of the sets you present.** Silence, "looks good", or your own confidence that a set is
obviously right are NOT a pick. Wait for an actual reply naming a set (or "merge these two",
"rename that one", "I'll choose myself"). Same discipline as the `sidegraph:ratify-decisions`
gate: present and wait, never auto-write.

## Flow

1. **Call `list_domain_candidates()`** with no filters — let yourself narrow in conversation,
   not the tool call. Its shape: `{graph_version, total_candidates, total_significant,
   truncated, already_claimed, skipped: {below_threshold, filtered}, groups: [{path,
   member_total, candidates: [{community, suggested_slug, suggested_title, members,
   top_members (<=3), top_file, has_label, anchor: {name, file_path} | null}, ...]}],
   ungrouped: [...same candidate shape...], note?}`. `anchor` is the field step 3 uses to
   build `seed_anchors` — the durable, name+file_path form of that candidate's membership,
   not the volatile `community` id next to it. `total_candidates` is how many candidates
   THIS response actually includes (post-limit, post-`already_claimed`); `total_significant`
   is the full count of significant communities before `limit` truncated them — the two can
   differ even when `truncated` is `false`. `note` explains the truncation (or a missing
   graph) and is only present when relevant. It writes nothing, ever — safe to re-read while
   you're still working. If `total_candidates <= ~20`, skip step 3's multi-set offering
   entirely: study the groups (step 2) and propose ONE sensible set, still gated by step 4.

2. **Study the groups before naming anything.** `suggested_slug`/`suggested_title` are the
   deterministic core's own best guess; `has_label` tells you whether a real engine label
   survived Gate-5 validation or the core fell back to a god-node/digest name. For any
   candidate that's ambiguous or you're not confident about, **read the code at `top_file` and
   look at `top_members`** before naming it. Never name a group from `has_label` alone — a
   label can be wrong (stale, mismatched, or naming a different community) — that's exactly
   why `has_label` and the fallback exist as a signal to check, not a source of truth.

3. **Compose 2-3 alternative sets of different granularity.** Typical shape on a mid-size repo:
   - **Coarse** (~8 domains) — merge sibling communities into top-level areas.
   - **Medium** (~15 domains) — one domain per natural cluster of communities.
   - **Fine** (~25 domains) — close to the raw communities, minimal merging.

   Each domain in each set needs:
   - a short human name (`title`),
   - a one-line WHY-IT-EXISTS `summary` — why this area exists, not a list of its members,
   - the exact `seed_anchors` it spans: for every candidate you're folding into this domain,
     take that candidate's own `anchor` field (`{"name", "file_path"}` — from
     `list_domain_candidates`' output, **not** its `community` id) and add it to the domain's
     `seed_anchors` list. **Copy the `anchor` object verbatim — never retype or guess a
     `file_path`.** An anchor whose `name`/`file_path` doesn't resolve is silently dropped
     (best-effort by design), so a mistyped path quietly gives that domain fewer members than
     you intended; use the candidate's `anchor` exactly as returned. A Coarse domain typically merges several candidates' anchors into
     one `seed_anchors: [...]` list; a Fine domain is usually just one candidate's single
     anchor. `community` ids are still useful for your OWN reasoning and for the table in step
     4 — just never put a raw `community` id into a draft; `seed_anchors` is the only
     membership seed that survives,
   - optional `path_prefixes` when the domain has a genuinely clean shared path prefix — leave
     it empty for a curated/merged domain with no single shared prefix; `seed_anchors` alone is
     sufficient membership.

   **Why `seed_anchors`, not `communities`:** community ids are volatile — Leiden renumbers
   them on every graph rebuild, and the committed store never records them directly — so a
   domain drafted with a raw `communities` list loses its membership the moment a teammate
   clones the repo or the graph gets rebuilt (name/summary survive; precise membership
   evaporates). `seed_anchors` anchors to durable entities instead — the same mechanism a
   decision's own anchors use — so membership survives a fresh clone or rebuild; `ratify`
   resolves `seed_anchors` into `communities` immediately.

   Use each group's shared `path` (and `ungrouped`) as your starting hint, but you're free to
   regroup, rename, split, or merge across group boundaries when the code says otherwise —
   that's the whole point of putting judgment at the edge.

4. **Present the sets as a compact table and STOP.**

   | Set | Domain | Communities | Why |
   |---|---|---|---|
   | Coarse | Order Execution | 40, 41, 55 | Places and manages live orders end-to-end |
   | Coarse | Market Data | 12, 13 | Ingests and normalizes exchange feeds |
   | Medium | Order Book | 40 | Maintains the local book from exchange deltas |
   | ... | | | |

   The `Communities` column is for human readability only — it's the `community` id(s) from
   the candidates you're folding in, so the human can cross-check against what they know of
   the code. The draft you write in step 5 carries each of those candidates' `anchor` as a
   `seed_anchors` entry instead, never the raw id shown here.

   Ask which set to use, or whether to merge/rename/drop specific domains first. **Do not call
   any write tool yet.**

5. **On an explicit pick**, call `propose_domains(drafts=[...])` with the chosen set — one
   draft per domain: `{"slug", "title", "summary", "seed_anchors": [...], "path_prefixes"?}`
   (no `parent_slug`: offered sets are flat — hierarchy is a `manage-domains` follow-up, not
   part of this flow). Then, only after that write, call
   `ratify(accept=[...])` with the returned `domain_id`s whose `ratified_by` is null (under
   `auto-all` the rest are already accepted, and `ratify` would return `error: … is not
   proposed` for them) —
   turning the pick into an accepted, TOC-visible set in the same turn (ratify also resolves
   each domain's `seed_anchors` into `communities` immediately, so `drill_down` shows real
   membership right away, without waiting for a separate sync). Provenance
   (`source="agent"` + session) makes the git diff show who named what.

6. **If the human says "I'll choose myself"** — or wants to hand-pick, edit, or reject
   individual domains instead of picking a whole set — stop here and point them at the
   `sidegraph:manage-domains` skill. Don't improvise ad hoc single-domain editing inside this
   flow.

## Notes

- Communities already claimed by an existing (non-superseded) domain never appear in
  `list_domain_candidates`' output — they're counted in `already_claimed` only. You're always
  naming what's still unclaimed.
- A large `total_significant` (hundreds, on a big repo) is normal — that volume is exactly why
  you compose merged Coarse/Medium sets instead of proposing one domain per raw candidate.
  `total_candidates` itself is capped by this skill's default call (`limit=100`), so it never
  exceeds 100 in one response — a large `total_significant` with `truncated: true` means there
  are more candidates beyond this response's top 100; narrow with `min_members`/`paths` or pass
  a wider `limit` to see them.
- **On a large repo, prefer `seed_anchors` and avoid broad `path_prefixes`.** A whole-package
  path like `path_prefixes: ["src"]` or `["tests"]` on a monorepo can match a huge fraction of
  the graph; sync's overbroad-path guard then drops that path rule (your `seed_anchors` still
  hold, but the path contributes nothing and is flagged as noise). For a big area, either leave
  `path_prefixes` empty and rely on `seed_anchors`, or split it into finer sub-domains with
  narrow prefixes (`src/tree`, `src/data`, `src/gbm`…) instead of one `src` domain.
- `sidegraph-domains bootstrap` (CLI) does the same deterministic candidate selection but skips
  the curation step — it's the scripted/CI alternative, not the recommended path for a human in
  a session. See [naming your domains](../../../../docs/guides/naming-your-domains.md).

## See also

- `sidegraph:manage-domains` — add/rename/drop a domain by hand, post-onboarding, or when the
  human wants to choose individually instead of picking a whole set.
- [`docs/guides/naming-your-domains.md`](../../../../docs/guides/naming-your-domains.md) — the
  human-facing walkthrough this skill is the primary path for.
